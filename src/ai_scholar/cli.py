"""CLI 入口。

子命令：`init` / `doctor` / `collect` / `export` / `site` / `enrich` / `enhance` /
`grade` / `analyze` / `mark` / `signals` / `relate` / `related` / `list` / `budget` /
`predict` / `stats` / `report`。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import create_engine
from sqlmodel import Session, col, select

from ai_scholar import __version__
from ai_scholar.config import (
    Settings,
    load_settings,
    load_watchlist,
    match_watchlist,
    project_root,
    watchlist_paths,
)
from ai_scholar.pipeline.export import resolve_out_dir
from ai_scholar.utils.errors import ConfigError, UnsafeOutputPath
from ai_scholar.utils.http import HttpClient, resolve_proxy

app = typer.Typer(
    name="ai-scholar",
    help="AI Scholar：采集 / 关联 / 推演 / 沉淀 AI 文献，输出 Obsidian 知识库。",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

ENV_SKELETON = """# AI Scholar 本地配置（★ 本文件不入库，已在 .gitignore 中排除）
#
# 不填也能跑：缺 api_key / base_url 时程序自动降级为 off 档（零 API 花费），不报错。

AI_SCHOLAR_MODE=fast
AI_SCHOLAR_LLM__BASE_URL=https://api.deepseek.com
AI_SCHOLAR_LLM__API_KEY=
"""


# ── init ────────────────────────────────────────────────────────────────
@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="已存在时也重写 .env 骨架"),
) -> None:
    """初始化本地环境：目录骨架 + `config/watchlist.local.yaml` + `.env` 骨架。"""
    root = project_root()
    settings = load_settings()

    for sub in ("reports", "signals", "cache", "logs", "tmp"):
        (settings.data_dir() / sub).mkdir(parents=True, exist_ok=True)
    console.print(f"[green]✓[/] 目录骨架已就绪：{settings.data_dir()}")

    default_path, local_path = watchlist_paths()
    if local_path.exists():
        console.print(f"[cyan]·[/] watchlist.local.yaml 已存在，跳过：{local_path}")
    elif default_path.exists():
        local_path.write_bytes(default_path.read_bytes())
        console.print(
            f"[green]✓[/] 已从示例复制个人 watchlist：[bold]{local_path}[/]\n"
            "    → 填入你自己的方向（该文件被 .gitignore 排除，不会入仓）"
        )
    else:
        console.print(f"[yellow]![/] 未找到示例 watchlist：{default_path}")

    env_path = root / ".env"
    if env_path.exists() and not force:
        console.print(f"[cyan]·[/] .env 已存在，跳过（要重写用 --force）：{env_path}")
    else:
        env_path.write_text(ENV_SKELETON, encoding="utf-8")
        console.print(f"[green]✓[/] 已生成 .env 骨架：{env_path}")

    console.print("\n下一步：[bold]ai-scholar doctor[/] 做一次环境体检。")


# ── doctor ──────────────────────────────────────────────────────────────
def _disk_free_gb(path: Path) -> float | None:
    try:
        usage = shutil.disk_usage(path if path.exists() else Path.home())
    except OSError:
        return None
    return usage.free / (1024**3)


def _display_path(path: Path) -> str:
    """日志脱敏：家目录以内显示成 `~/...`（★ CI 日志默认公开）。"""
    try:
        return "~/" + str(path.resolve().relative_to(Path.home()))
    except (ValueError, OSError):
        return str(path)


def _git_remote_names(path: Path) -> set[str] | None:
    """读 vault 的 git remote 名（不联网）。失败返回 None。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "remote"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _vault_row(settings: Settings) -> tuple[str, str, str]:
    """vault 状态：★ 无 remote 是 warning（笔记丢了就是真丢），不是 error。"""
    try:
        path = resolve_out_dir(None, settings)
    except UnsafeOutputPath as exc:
        return "vault", "[red]危险[/]", str(exc)

    shown = _display_path(path)
    if not path.exists():
        return (
            "vault",
            "[yellow]warning[/]",
            f"{shown} 不存在（首次 export 会创建；建议先 git init + private remote）",
        )
    if not (path / ".git").exists():
        return "vault", "[yellow]warning[/]", f"{shown} 未初始化 git —— ★ 笔记无备份"

    remotes = _git_remote_names(path)
    if not remotes:
        return "vault", "[yellow]warning[/]", f"{shown} 无 git remote —— ★ 笔记无备份"
    return "vault", "[green]OK[/]", f"{shown} · remote: {', '.join(sorted(remotes))}"


async def _healthcheck(name: str, url: str, params: dict[str, str], rate_limit: str) -> str:
    """对某个源发一个**最小的**真实请求，返回人类可读结论。

    ★ 失败只写 warning，不影响 doctor 的退出码（单源异常不得打断全局）。
    """
    async with HttpClient() as client:
        try:
            response = await client.get(url, params=params, source=name, rate_limit=rate_limit)
        except (httpx.HTTPError, OSError) as exc:  # 网络不可达 / DNS / 超时
            return f"不可达（{type(exc).__name__}）"
        return f"HTTP {response.status_code} · {len(response.content)} B"


@app.command()
def doctor(
    skip_healthcheck: bool = typer.Option(
        False, "--skip-healthcheck", help="跳过各数据源连通性检查（离线时用）"
    ),
) -> None:
    """环境体检：Python / LLM / 代理 / 磁盘 / watchlist / 各数据源。

    ★ 未配 key 时提示"自动降级 off"，退出码仍为 0。
    """
    settings = load_settings()
    table = Table(title=f"ai-scholar doctor · v{__version__}", title_justify="left")
    table.add_column("检查项")
    table.add_column("状态")
    table.add_column("说明")

    py_ok = sys.version_info >= (3, 11)
    table.add_row(
        "Python 版本",
        "[green]OK[/]" if py_ok else "[red]过低[/]",
        f"{sys.version.split()[0]}（要求 ≥ 3.11）",
    )

    mode = settings.effective_mode()
    has_key = bool(settings.llm.api_key.strip())
    has_base = bool(settings.llm.base_url.strip())
    if has_key and has_base:
        llm_state, llm_note = "[green]OK[/]", f"档位 {mode.value}（fast={settings.llm.fast_model}）"
    else:
        missing = " / ".join(
            [n for n, ok in (("api_key", has_key), ("base_url", has_base)) if not ok]
        )
        # ★ 缺配置是预期状态，不是错误：自动降级 off，退出码 0
        llm_state = "[yellow]自动降级 off[/]"
        llm_note = f"缺 {missing}；不配 key 也能跑采集与导出（LLM 增强跳过）"
    table.add_row("LLM 配置", llm_state, llm_note)

    proxy = resolve_proxy()
    table.add_row("代理", "[cyan]已设置[/]" if proxy else "[cyan]未设置[/]", proxy or "直连")

    data_dir = settings.data_dir()
    free_gb = _disk_free_gb(data_dir)
    free_text = f" · 可用 {free_gb:.1f} GB" if free_gb is not None else ""
    if data_dir.exists():
        table.add_row("数据目录", "[green]OK[/]", f"{_display_path(data_dir)}{free_text}")
    else:
        table.add_row(
            "数据目录",
            "[yellow]缺失[/]",
            f"{_display_path(data_dir)} 不存在；运行 `ai-scholar init` 创建{free_text}",
        )

    _default_path, local_path = watchlist_paths()
    watchlist = load_watchlist()
    if local_path.exists():
        table.add_row(
            "watchlist",
            "[green]OK[/]",
            f"{local_path.name} · {len(watchlist.groups)} 组 / {watchlist.total_keywords()} 关键词",
        )
    else:
        # ★ warning 不是 error：用仓库里那份通用示例也能跑
        table.add_row(
            "watchlist",
            "[yellow]warning[/]",
            f"无 {local_path.name}；当前用通用示例 {len(watchlist.groups)} 组 / "
            f"{watchlist.total_keywords()} 关键词（ai-scholar init 可复制一份）",
        )

    table.add_row(*_vault_row(settings))

    if skip_healthcheck:
        table.add_row("数据源", "[cyan]已跳过[/]", "--skip-healthcheck")
    else:
        from ai_scholar.utils.timeutil import today_local

        def endpoint_of(name: str) -> str:
            cfg = settings.source(name)
            if cfg is None:
                return ""
            # HF 用 hf_base_url（默认官方，可被镜像覆盖）；arXiv 用 endpoint
            return cfg.hf_url() if name == "hf_daily" else cfg.endpoint

        day = today_local().isoformat()
        checks: list[tuple[str, str, dict[str, str], str]] = [
            (
                "arxiv",
                endpoint_of("arxiv"),
                {"search_query": "cat:cs.AI", "max_results": "1"},
                "1/3s",
            ),
            ("hf_daily", endpoint_of("hf_daily"), {"date": day}, "10/1s"),
        ]
        for name, url, params, rate in checks:
            if not url:
                table.add_row(f"数据源 {name}", "[yellow]warning[/]", "未配置 endpoint")
                continue
            try:
                result = asyncio.run(_healthcheck(name, url, params, rate))
            except Exception as exc:  # noqa: BLE001 —— 体检不得因异常中断
                result = f"检查失败（{type(exc).__name__}: {exc}）"
            state = "[green]OK[/]" if result.startswith("HTTP 2") else "[yellow]warning[/]"
            table.add_row(f"数据源 {name}", state, result)

    console.print(table)
    if mode == "off":
        console.print(
            "[yellow]提示[/]：当前为 off 档（零 API 花费）。"
            "在 .env 里填 AI_SCHOLAR_LLM__API_KEY 即可启用 LLM 增强。"
        )
    raise typer.Exit(0)


# ── 以下子命令为 L0-A 的**刻意占位** ──────────────────────────────────────
@app.command()
def collect(
    days: int | None = typer.Option(
        None, "--days", "-d", help="回溯天数（默认取 sources.arxiv.lookback_days）"
    ),
    source: str | None = typer.Option(None, "--source", "-s", help="只采一个源：arxiv | hf_daily"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只统计，不写库 / 不写 JSONL"),
) -> None:
    """采集：arXiv + HF Daily Papers → 三级去重 → 入库 + `first_seen.jsonl`。

    ★ 单源失败只 warning（写进 `runs.degraded_json`），**退出码仍为 0**。
    ★ 默认回看窗口取 `sources.arxiv.lookback_days`（索引滞后 3–7 天，只采当天会漏采）。
    """
    from ai_scholar.pipeline.collect import run_collect

    if source is not None and source not in ("arxiv", "hf_daily"):
        console.print(f"[red]未知源[/]：{source}（可选 arxiv / hf_daily）")
        raise typer.Exit(2)

    settings = load_settings()
    effective_days = days if days is not None else settings.lookback_days()
    report = asyncio.run(run_collect(days=effective_days, source=source, dry_run=dry_run))

    table = Table(
        title=f"collect · run {report.run_id}" + ("（dry-run）" if dry_run else ""),
        title_justify="left",
    )
    table.add_column("源")
    table.add_column("fetched")
    table.add_column("new")
    table.add_column("updated")
    table.add_column("deduped")
    table.add_column("状态")
    for outcome in report.outcomes:
        state = f"[yellow]warning {outcome.degraded}[/]" if outcome.failed else "[green]OK[/]"
        table.add_row(
            outcome.name,
            str(outcome.fetched),
            str(outcome.new),
            str(outcome.updated),
            str(outcome.deduped),
            state,
        )
    console.print(table)
    console.print(
        f"合计 fetched={report.fetched} new={report.new} "
        f"updated={report.updated} deduped={report.deduped}"
    )
    console.print(f"first_seen.jsonl 新增 {report.first_seen_new} 行（append-only，已存在不覆盖）")
    if not dry_run:
        console.print(f"db：{report.db_path}")
    if report.degraded:
        console.print("[yellow]提示[/]：部分源降级（已写入 runs.degraded_json），不影响退出码。")
    raise typer.Exit(0)


@app.command()
def export(
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="导出条数（默认读 export.default_limit）"
    ),
    ids: str | None = typer.Option(None, "--ids", help="只导这些 id（逗号分隔，db 主键）"),
    since: str | None = typer.Option(None, "--since", help="只导 first_seen >= 该日期"),
    out: Path | None = typer.Option(None, "--out", help="vault 输出目录（必须在代码仓库之外）"),
    check: bool = typer.Option(
        False, "--check", help="只比对不写盘：列出会变更的文件；有变更退出 1，无变更退出 0"
    ),
    all_papers: bool = typer.Option(
        False,
        "--all",
        help="导出最新 N 篇（demo/调试）；★ 默认只导出 selected；与 --check 合用则校验全量笔记",
    ),
) -> None:
    """导出：db → Obsidian vault（合成文档：元信息 + why + 我的笔记区）。

    ★ 用户区永不覆盖（标记块）；★ **默认只导出 `selected=1`**（§5.6 v1.7）。
    ★ `--check` 把幂等变成**可断言**的检查（真 vault 不在版本控制里，git diff 看不到它）。
      比对范围 = 文献笔记 + 首页 + links.json；生成报告是运行日志，允许 diff。
    """
    from ai_scholar.pipeline.export import run_export

    since_date = None
    if since:
        try:
            since_date = date.fromisoformat(since)
        except ValueError:
            console.print(f"[red]日期格式错误[/]：{since}（应为 YYYY-MM-DD）")
            raise typer.Exit(2) from None

    id_list = [s.strip() for s in ids.split(",") if s.strip()] if ids else None
    report = run_export(
        limit=limit,
        ids=id_list,
        since=since_date,
        out=out,
        check=check,
        all_papers=all_papers,
    )

    if report.empty_selection:
        console.print(
            "[yellow]尚无选中论文[/]：默认只导出 `selected=1`。\n"
            "  · 用 `ai-scholar mark <paper-id>` 收藏要读的论文\n"
            "  · 或 `ai-scholar export --all` 导出最新 N 篇（demo/调试）"
        )
        raise typer.Exit(0)

    if check:
        if report.changed:
            console.print(f"[yellow]将变更 {len(report.changed)} 个文件[/]（首次导出或幂等被破坏）")
            for path in report.changed:
                try:
                    shown = path.relative_to(report.vault_dir)
                except ValueError:
                    shown = path
                console.print(f"  · {shown}")
            raise typer.Exit(1)
        console.print(f"[green]✓[/] 无变更（{report.exported} 篇，幂等）")
        raise typer.Exit(0)

    console.print(
        f"[green]✓[/] 导出 {report.exported} 篇"
        f"（库内共 {report.total_in_db} 篇）→ {_display_path(report.vault_dir)}"
    )
    if report.degraded:
        for key, reason in report.degraded.items():
            console.print(f"[yellow]warning[/] {key}：{reason}")
    console.print(
        "说明：正文用标记块保护，`## 我的笔记` 区永不覆盖；"
        "导出时间（秒级）只进 `90-元数据/生成报告.md`。"
    )
    raise typer.Exit(0)


@app.command()
def site(
    out: Path | None = typer.Option(
        None, "--out", help="输出目录（默认 export.site_dir），产物为其中的 index.html"
    ),
    days: int | None = typer.Option(
        None, "--days", help="趋势区展示最近几天报告（默认 export.site_days）"
    ),
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="单页论文条数上限（默认 export.site_max_papers）"
    ),
    check: bool = typer.Option(False, "--check", help="只比对不写盘：有变更退出 1，无变更退出 0"),
) -> None:
    """生成静态 HTML 单文件站点（L5a）：只读报告 / 记分板 / DB 元数据。

    ★ 不读 vault、不调 LLM、不联网；页面自包含（CSS 内联、无 CDN），双击即可打开。
    ★ 重跑逐字节一致（页面不含生成时间）；分项分只展示、不合并为单一分数。
    """
    from ai_scholar.pipeline.site import run_site

    report = run_site(out=out, days=days, limit=limit, check=check)

    if check:
        if report.changed:
            console.print(
                f"[yellow]将变更[/] {_display_path(report.out_path)}（首次生成或输入已变）"
            )
            raise typer.Exit(1)
        console.print(f"[green]✓[/] 无变更（{report.date}，幂等）")
        raise typer.Exit(0)

    console.print(f"[green]✓[/] 生成静态站点 → {_display_path(report.out_path)}")
    console.print(
        f"  数据日期 {report.date} · 论文 {report.papers} 篇"
        f" · 报告 {report.reports} 天 · {report.size_bytes / 1024:.1f} KB"
    )
    for key, reason in report.degraded.items():
        console.print(f"[yellow]warning[/] {key}：{reason}")
    console.print("说明：只读 public 数据（不含个人笔记 / 正文）；分项分不合并为单一分数。")
    raise typer.Exit(0)


@app.command()
def enrich(
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="处理条数（默认 enrich.default_limit）"
    ),
    force: bool = typer.Option(False, "--force", help="忽略 enrich_state，重查全部"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只统计，不写库"),
) -> None:
    """补齐引用：S2 batch → `paper_links` 出边（入边由反向查询得到）。

    ★ 只写**库内目标**的边（库外目标只累加 `refs_count`，不产生幽灵条目）。
    ★ 冷启动期边必然稀疏（§5.1b）—— 这是预期，不是失败。
    ★ S2 失败只 warning（写进 degraded），**退出码仍为 0**。
    """
    from ai_scholar.pipeline.enrich import run_enrich

    settings = load_settings()
    effective = limit if limit is not None else settings.enrich.default_limit
    report = asyncio.run(run_enrich(limit=effective, force=force, dry_run=dry_run))

    table = Table(title=f"enrich · run {report.run_id}", title_justify="left")
    table.add_column("项")
    table.add_column("值")
    table.add_row("本次处理论文", str(report.requested))
    table.add_row("S2 命中（解析出 paperId）", str(report.fetched))
    table.add_row("references 总条数", str(report.refs_seen))
    table.add_row("其中命中库内论文", str(report.in_library_hits))
    table.add_row("出边（新增）", str(report.out_links))
    table.add_row("被引用条目（distinct dst）", str(report.cited_papers))
    table.add_row("done / pending / stale", f"{report.done} / {report.pending} / {report.stale}")
    console.print(table)
    if report.degraded:
        for key, reason in report.degraded.items():
            console.print(f"[yellow]warning[/] {key}：{reason}")
    console.print(
        "说明：入边**不单独存储**（查 dst 即得）；库外被引目标不写边，只计入 refs_count。"
    )
    raise typer.Exit(0)


@app.command()
def enhance(
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="增强论文数（默认 enhance.default_limit）"
    ),
    force: bool = typer.Option(False, "--force", help="忽略 enhance_content_hash，重做全部"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只统计，不写库 / 不调 LLM"),
) -> None:
    """LLM 增强（fast）：一句话 / 为什么重要 / 方法标签 + 强相似边关联解读。

    ★ off 档零调用；预算闸门超限即停；结果持久化（同条目不重复生成）。
    ★ 失败只 warning（写进 degraded），**退出码仍为 0**。
    """
    from ai_scholar.pipeline.enhance import run_enhance

    report = asyncio.run(run_enhance(limit=limit, force=force, dry_run=dry_run))

    table = Table(title=f"enhance · run {report.run_id}（{report.mode}）", title_justify="left")
    table.add_column("项")
    table.add_column("值")
    table.add_row("本次选中", str(report.requested))
    table.add_row("增强成功", str(report.enhanced))
    table.add_row("预算拦截", str(report.blocked))
    table.add_row("已增强跳过", str(report.skipped))
    table.add_row("关联解读", str(report.links_interpreted))
    table.add_row("LLM 调用", str(report.llm_calls))
    table.add_row("成本（元）", f"{report.cost_cny:.4f}")
    console.print(table)
    if report.degraded:
        for key, reason in report.degraded.items():
            console.print(f"[yellow]warning[/] {key}：{reason}")
    raise typer.Exit(0)


@app.command()
def analyze(
    ids: str | None = typer.Option(None, "--ids", help="显式指定论文 id（逗号分隔）"),
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="分析篇数（默认 deep_analysis.default_limit）"
    ),
    force: bool = typer.Option(False, "--force", help="忽略 deep_content_hash，重做全部"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只统计，不写库 / 不调 LLM"),
) -> None:
    """深度分析（strong）：三句半 + 六段 + 背景透镜。

    ★ 通道 A（意图触发，不看 Tier）+ B（Tier1 补读）；周配额 5 篇。
    ★ 正文临时抓取、抓完即弃；off 档零调用；失败只 warning，退出码仍 0。
    """
    from ai_scholar.pipeline.deep_analysis import run_deep_analysis

    id_list = [s.strip() for s in ids.split(",") if s.strip()] if ids else None
    report = asyncio.run(run_deep_analysis(ids=id_list, limit=limit, force=force, dry_run=dry_run))

    table = Table(title=f"analyze · run {report.run_id}（{report.mode}）", title_justify="left")
    table.add_column("项")
    table.add_column("值")
    table.add_row("本次选中", str(report.requested))
    table.add_row("分析成功", str(report.analyzed))
    table.add_row("预算/配额拦截", str(report.blocked))
    table.add_row("已分析跳过", str(report.skipped))
    table.add_row("全文命中", str(report.fulltext_hits))
    table.add_row("LLM 调用", str(report.llm_calls))
    table.add_row("成本（元）", f"{report.cost_cny:.4f}")
    console.print(table)
    if report.degraded:
        for key, reason in report.degraded.items():
            console.print(f"[yellow]warning[/] {key}：{reason}")
    raise typer.Exit(0)


@app.command()
def grade(
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="定级论文数（默认 grade.default_limit）"
    ),
    force: bool = typer.Option(False, "--force", help="忽略 grade_content_hash，重做全部"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只统计，不写库 / 不调 LLM"),
) -> None:
    """文献定级（fast）：三判据 → Tier 1/2/3。

    ★ 延迟定级：只处理成熟或引用达标的论文；结果持久化，同条目不重复定级。
    ★ off 档零调用；预算闸门超限即停；失败只 warning，退出码仍 0。
    """
    from ai_scholar.pipeline.grade import run_grade

    report = asyncio.run(run_grade(limit=limit, force=force, dry_run=dry_run))

    table = Table(title=f"grade · run {report.run_id}（{report.mode}）", title_justify="left")
    table.add_column("项")
    table.add_column("值")
    table.add_row("本次选中", str(report.requested))
    table.add_row("定级成功", str(report.graded))
    table.add_row("预算拦截", str(report.blocked))
    table.add_row("已定级跳过", str(report.skipped))
    table.add_row("LLM 调用", str(report.llm_calls))
    table.add_row("成本（元）", f"{report.cost_cny:.4f}")
    console.print(table)
    if report.degraded:
        for key, reason in report.degraded.items():
            console.print(f"[yellow]warning[/] {key}：{reason}")
    raise typer.Exit(0)


@app.command()
def mark(
    paper_id: str,
    unmark: bool = typer.Option(False, "--unmark", help="取消收藏"),
) -> None:
    """显式收藏 / 取消收藏（★ 决定 `export` 是否为该篇生成笔记）。"""
    from ai_scholar.store.models import Paper

    settings = load_settings()
    engine = create_engine(f"sqlite:///{settings.db_path()}", echo=False)
    with Session(engine) as session:
        paper = session.get(Paper, paper_id)
        if paper is None:
            console.print(f"[red]未知论文 id[/]：{paper_id}")
            raise typer.Exit(2)
        paper.selected = not unmark
        title = paper.title[:60]
        session.commit()
    console.print(f"[green]✓[/] {'取消收藏' if unmark else '已收藏'}：{paper_id} — {title}")
    raise typer.Exit(0)


@app.command()
def signals(
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="本次最多处理多少篇（默认 signals.default_limit）"
    ),
    force: bool = typer.Option(False, "--force", help="忽略 signal_next_at，全部采样一次"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只统计，不写库 / 不写 JSONL"),
) -> None:
    """按时序状态机采样一次：S2 batch → `signals` 表 + `data/signals/YYYY-MM.jsonl`。

    ★ 状态持久化（`signal_next_at`），**不是每日全量重算**。
    ★ 两套时钟：成熟度用 `published_at`，观测窗口用 `first_seen`。
    ★ S2 失败只 warning，退出码 0；off 档零 LLM 调用。
    """
    from ai_scholar.pipeline.signals import run_signals

    settings = load_settings()
    effective = limit if limit is not None else settings.signals.default_limit
    report = asyncio.run(run_signals(limit=effective, force=force, dry_run=dry_run))

    table = Table(
        title=f"signals · {report.today} · run {report.run_id}"
        + ("（dry-run）" if dry_run else ""),
        title_justify="left",
    )
    table.add_column("项")
    table.add_column("值")
    table.add_row("补跑预筛（历史行）", str(report.backfilled))
    table.add_row("本次到期", str(report.due))
    table.add_row("S2 命中", str(report.fetched))
    table.add_row("signals 新增行", str(report.rows_new))
    table.add_row("已存在（幂等跳过）", str(report.rows_existing))
    table.add_row("耗时", f"{report.duration_s}s")
    console.print(table)

    states = Table(title="状态分布", title_justify="left")
    states.add_column("signal_state")
    states.add_column("数量")
    for state, count in sorted(report.states.items()):
        states.add_row(state or "(未预筛)", str(count))
    console.print(states)

    if report.events:
        events = " · ".join(f"{k}={v}" for k, v in sorted(report.events.items()))
        console.print(f"状态机事件：{events}")
    if report.degraded:
        for key, reason in report.degraded.items():
            console.print(f"[yellow]warning[/] {key}：{reason}")
    if not dry_run:
        console.print(f"JSONL：{report.jsonl_path}")
    raise typer.Exit(0)


@app.command()
def relate(
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="只处理最近 N 篇（小规模验证用；默认全量）"
    ),
    kind: str = typer.Option("all", "--type", help="bibcoupling | similarity | all"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只统计，不写库"),
) -> None:
    """计算关联：文献耦合（倒排索引）+ TF-IDF 语义相似（增量）。★ 零 LLM。

    ★ 文献耦合按 `ref_external_id` 分桶，只比较**同桶**论文对，绝不做全库两两比较。
    ★ 相似度只算 `similar_at IS NULL` 的论文 vs 存量；无 sklearn 时降级跳过。
    """
    from ai_scholar.pipeline.relate import KINDS, run_relate

    if kind not in (*KINDS, "all"):
        console.print(f"[red]未知类型[/]：{kind}（可选 {' / '.join(KINDS)} / all）")
        raise typer.Exit(2)

    report = run_relate(kind=kind, limit=limit, dry_run=dry_run)

    table = Table(
        title=f"relate · {report.kind} · run {report.run_id}" + ("（dry-run）" if dry_run else ""),
        title_justify="left",
    )
    table.add_column("项")
    table.add_column("值")
    table.add_row("参与论文", str(report.papers))
    table.add_row("引用行（paper_references）", str(report.ref_rows))
    table.add_row("倒排桶（≥2 篇）", str(report.buckets))
    table.add_row("桶内对数 ΣC(n,2)", str(report.pairs_compared))
    table.add_row("平均共现数", str(report.avg_cooccur))
    table.add_row("相似度比对文档", str(report.docs))
    for name in sorted(set(report.edges_new) | set(report.edges_existing)):
        table.add_row(
            f"边 {name}（新增 / 已存在）",
            f"{report.edges_new.get(name, 0)} / {report.edges_existing.get(name, 0)}",
        )
    table.add_row("耗时", f"{report.duration_s}s")
    console.print(table)
    console.print("说明：对称边只存一条（src<dst），渲染时双向展开；不会把「文献耦合」误标成引用。")
    if report.degraded:
        for key, reason in report.degraded.items():
            console.print(f"[yellow]warning[/] {key}：{reason}")
    raise typer.Exit(0)


def _titles_for(session: Session, ids: list[str]) -> dict[str, str]:
    """id → 标题（`related` 展示用，避免只显示内部主键）。"""
    from ai_scholar.store.models import Paper

    if not ids:
        return {}
    rows = session.exec(select(Paper).where(col(Paper.id).in_(set(ids)))).all()
    return {p.id: p.title for p in rows}


@app.command()
def related(paper_id: str) -> None:
    """查看某篇论文的关联（★ 分「引用了」「被引用」两栏，用于人工验证方向）。

    ★ 方向语义：`paper_links(src, dst, references)` == **src 引用了 dst**。
      左边查 `src_id`，右边查 `dst_id`，两者必须互为反向。
    """
    from ai_scholar.store.models import LINK_TYPE_REFERENCES, Paper, PaperLink

    settings = load_settings()
    engine = create_engine(f"sqlite:///{settings.db_path()}", echo=False)
    with Session(engine) as session:
        paper = session.get(Paper, paper_id)
        if paper is None:
            console.print(f"[red]未知论文 id[/]：{paper_id}")
            raise typer.Exit(2)

        out_rows = list(
            session.exec(
                select(PaperLink).where(
                    PaperLink.src_id == paper_id,
                    PaperLink.link_type == LINK_TYPE_REFERENCES,
                )
            ).all()
        )
        in_rows = list(
            session.exec(
                select(PaperLink).where(
                    PaperLink.dst_id == paper_id,
                    PaperLink.link_type == LINK_TYPE_REFERENCES,
                )
            ).all()
        )

    titles = {}
    with Session(engine) as session:
        titles = _titles_for(session, [r.dst_id for r in out_rows] + [r.src_id for r in in_rows])

    table = Table(
        title=f"关联 · {paper.title[:60]}",
        title_justify="left",
        caption="★ 左栏 = 它引用了谁（出边）；右栏 = 谁引用了它（入边，反向查询得到）",
    )
    table.add_column(f"引用了（{len(out_rows)}）", max_width=60)
    table.add_column(f"被引用（{len(in_rows)}）", max_width=60)
    rows = max(len(out_rows), len(in_rows))
    for i in range(rows):
        left = ""
        if i < len(out_rows):
            dst = out_rows[i].dst_id
            left = titles.get(dst, dst)
        right = ""
        if i < len(in_rows):
            src = in_rows[i].src_id
            right = titles.get(src, src)
        table.add_row(left, right)
    if rows == 0:
        table.add_row("（暂无 — 冷启动期必然稀疏）", "（暂无 — 需等引用它的论文入库）")
    console.print(table)
    raise typer.Exit(0)


@app.command("list")
def list_papers(
    limit: int = typer.Option(20, "--limit", "-n", help="显示条数"),
    since: str | None = typer.Option(None, "--since", help="只显示 first_seen >= 该日期"),
    watchlist_only: bool = typer.Option(
        False, "--watchlist-only", help="只显示 watchlist 命中的（★ 展示筛选，不改库）"
    ),
    score: bool = typer.Option(
        False, "--score", help="显示客观信号分项（热度/跨源/主题；★ 不合成单一分）"
    ),
) -> None:
    """列出库内论文，**★ 显示 watchlist 命中标记**。

    ★ `--watchlist-only` 只是**展示筛选**，不改变库、不影响入库。
    ★ 函数名不能叫 `list`——那会遮蔽内建的 `list` 类型（mypy strict 直接报错）。
    """
    from ai_scholar.pipeline.export import select_papers

    settings = load_settings()
    _default_path, local_path = watchlist_paths()
    if not local_path.exists():
        console.print(
            f"[yellow]warning[/] 未找到 {local_path.name}；"
            "当前用仓库里的通用示例（`ai-scholar init` 可复制一份）"
        )

    since_date = None
    if since:
        try:
            since_date = date.fromisoformat(since)
        except ValueError:
            console.print(f"[red]日期格式错误[/]：{since}（应为 YYYY-MM-DD）")
            raise typer.Exit(2) from None

    engine = create_engine(f"sqlite:///{settings.db_path()}", echo=False)
    watchlist = load_watchlist()
    table = Table(title=f"库内论文（{settings.db_path().name}）", title_justify="left")
    table.add_column("★")
    table.add_column("id")
    table.add_column("first_seen")
    table.add_column("title", max_width=60)
    table.add_column("HF")
    if score:
        table.add_column("热度")
        table.add_column("跨源")
        table.add_column("主题")

    shown_papers = []
    with Session(engine) as session:
        # ★ `list` 是"看库里有什么"，不受选集限制（export 才默认只导 selected）
        papers = select_papers(session, limit=limit, since=since_date, all_papers=True)
        for paper in papers:
            hit = match_watchlist(watchlist, paper.title, paper.abstract or "")
            if watchlist_only and not hit.matched:
                continue
            shown_papers.append((paper, hit.matched))

    if score:
        from ai_scholar.pipeline.score import fmt, score_many

        breakdowns = score_many([paper for paper, _ in shown_papers], watchlist)
    for paper, matched in shown_papers:
        row = [
            "★" if matched else "",
            paper.id,
            (paper.first_seen_at or "")[:10],
            paper.title[:60],
            str(paper.hf_score) if paper.hf_score is not None else "",
        ]
        if score:
            item = breakdowns[paper.id]
            row += [fmt(item.heat), fmt(item.cross_source), fmt(item.topic)]
        table.add_row(*row)
    console.print(table)
    console.print(f"显示 {len(shown_papers)} 条（★ = watchlist 命中）")
    if score:
        console.print("分项分只在本次结果内归一化，不可跨批比较；★ 不合成单一总分")
    raise typer.Exit(0)


@app.command()
def budget(
    input_tokens: int = typer.Option(..., "--input-tokens", help="输入 token 估算"),
    max_tokens: int = typer.Option(..., "--max-tokens", help="输出 token 上限（max_tokens）"),
    tier: str = typer.Option("fast", "--tier", help="档位：fast | strong"),
    spent_daily: float = typer.Option(0.0, "--spent-daily", help="当日已花费（元）"),
    spent_weekly: float = typer.Option(0.0, "--spent-weekly", help="本周已花费（元）"),
    spent_monthly: float = typer.Option(0.0, "--spent-monthly", help="本月已花费（元）"),
    spent_yearly: float = typer.Option(0.0, "--spent-yearly", help="本年已花费（元）"),
    calls_this_run: int = typer.Option(0, "--calls-this-run", help="本次 run 已调用次数"),
) -> None:
    """预算演练：估算一次 LLM 调用成本并过闸门（★ 不真正调 LLM，零网络）。

    ★ 闸门在调用前拦截（成本预算 §3）：钱不够 = 进队列，**不截断生成**。
    ★ 拦截不是错误 —— 退出码仍为 0。
    """
    from ai_scholar.budget import BudgetState, check_budget, estimate_cost

    if tier not in ("fast", "strong"):
        console.print(f"[red]未知档位[/]：{tier}（可选 fast / strong）")
        raise typer.Exit(2)

    settings = load_settings()
    model = settings.llm.fast_model if tier == "fast" else settings.llm.strong_model
    provider = settings.llm.provider

    estimate = estimate_cost(settings.budget, provider, model, input_tokens, max_tokens)
    state = BudgetState(
        spent_daily=spent_daily,
        spent_weekly=spent_weekly,
        spent_monthly=spent_monthly,
        spent_yearly=spent_yearly,
        calls_this_run=calls_this_run,
    )
    decision = check_budget(settings.budget, state, estimate)

    table = Table(title=f"budget · {provider} / {model}", title_justify="left")
    table.add_column("项")
    table.add_column("值")
    table.add_row("档位", tier)
    table.add_row("输入 token（按未命中计价）", str(input_tokens))
    table.add_row("输出上限（max_tokens）", str(max_tokens))
    table.add_row("预估成本", f"¥{estimate.cost_cny:.4f}")
    table.add_row(
        "日额度",
        f"¥{spent_daily:.4f} + ¥{estimate.cost_cny:.4f} / ¥{settings.budget.daily_limit_cny}",
    )
    table.add_row(
        "单次 run 调用次数",
        f"{calls_this_run} + 1 / {settings.budget.max_calls_per_run}",
    )
    console.print(table)

    if decision.allowed:
        console.print(f"[green]✓ 放行[/]：预估 ¥{estimate.cost_cny:.4f}")
    else:
        console.print(f"[red]✗ 拦截[/]：{decision.reason}（任务进队列，不截断生成）")
    raise typer.Exit(0)


@app.command()
def predict(
    limit: int | None = typer.Option(None, "--limit", help="本次最多新增条数"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只预览，不写记分板"),
    insight: list[str] | None = typer.Option(
        None, "--insight", help="记录一条长期观点（★ 不进记分板、不计入统计）"
    ),
) -> None:
    """生成可验证推演（★ 零 LLM）：写入 `data/predictions/scoreboard.jsonl`。"""
    from ai_scholar.pipeline.predict import run_predict

    settings = load_settings()
    report = run_predict(
        settings=settings,
        dry_run=dry_run,
        limit=limit,
        insights=insight,
    )

    table = Table(title=f"predict · {report.today}", title_justify="left")
    table.add_column("类型")
    table.add_column("目标")
    table.add_column("阈值")
    table.add_column("到期")
    table.add_column("判断", max_width=58)
    for prediction in report.predictions:
        table.add_row(
            prediction.criterion.type,
            prediction.criterion.target,
            str(prediction.criterion.threshold),
            prediction.due_at,
            prediction.claim,
        )
    console.print(table)

    title = "预览（未写入）" if dry_run else "新增"
    console.print(
        f"{title} {report.created} 条 · 跳过 {report.skipped} 条 · 观点 {report.opinions} 条"
    )
    console.print(f"记分板：{report.scoreboard_path}")
    if report.degraded:
        for key, reason in report.degraded.items():
            console.print(f"[yellow]warning[/] {key}：{reason}")
    for rejected in report.rejected:
        console.print(f"[red]拒绝[/] {rejected}（未通过可证伪门禁）")
    raise typer.Exit(0)


@app.command()
def stats(
    verify: bool = typer.Option(False, "--verify", help="验证所有到期推演（零网络）"),
    calibration: bool = typer.Option(False, "--calibration", help="定级校准（Tier vs 引用）"),
    impact: bool = typer.Option(False, "--impact", help="被引增长与开创/跟风候选"),
    limit: int = typer.Option(20, "--limit", help="影响分析显示条数"),
) -> None:
    """记分板统计：命中率只算 hit/miss，`insufficient` 与观点都不进分母。"""
    from ai_scholar.pipeline.stats import (
        calibration as run_calibration,
    )
    from ai_scholar.pipeline.stats import (
        impact as run_impact,
    )
    from ai_scholar.pipeline.stats import (
        summarize,
        verify_due,
    )
    from ai_scholar.utils.timeutil import today_local

    settings = load_settings()
    today = today_local()

    if verify:
        report = verify_due(settings=settings, today=today)
        table = Table(title=f"stats --verify · {today}", title_justify="left")
        table.add_column("状态")
        table.add_column("判据")
        table.add_column("观测 / 阈值")
        table.add_column("判断", max_width=50)
        for detail in report.details:
            observed = "—" if detail.observed is None else str(detail.observed)
            table.add_row(
                detail.status,
                detail.id,
                f"{observed} / {detail.threshold}",
                detail.note or detail.claim,
            )
        console.print(table)
        console.print(
            f"验证 {report.checked} 条：hit {report.hit} · miss {report.miss}"
            f" · insufficient {report.insufficient}"
        )
        for key, reason in report.degraded.items():
            console.print(f"[yellow]warning[/] {key}：{reason}")

    summary = summarize(settings=settings, today=today)
    hit_rate = "—" if summary.hit_rate is None else f"{summary.hit_rate:.1%}"
    table = Table(title="记分板", title_justify="left")
    table.add_column("项")
    table.add_column("值")
    table.add_row("未到期 / 到期未验证", f"{summary.open}（其中已到期 {summary.due_open}）")
    table.add_row("命中 / 未命中", f"{summary.hit} / {summary.miss}")
    table.add_row("命中率（不含 insufficient）", hit_rate)
    table.add_row("数据不足（不计入分母）", str(summary.insufficient))
    table.add_row("观点（★ 不进统计）", str(summary.opinions))
    console.print(table)

    if calibration:
        if not settings.db_path().exists():
            console.print("[yellow]warning[/] 数据库不存在，跳过定级校准")
        else:
            engine = create_engine(f"sqlite:///{settings.db_path()}", echo=False)
            with Session(engine) as session:
                cal = run_calibration(session, settings.calibration, today=today)
            table = Table(title="定级校准（成熟论文当前引用）", title_justify="left")
            table.add_column("Tier")
            table.add_column("n")
            table.add_column("引用均值")
            table.add_column("引用中位")
            for cal_row in cal.rows:
                table.add_row(
                    f"Tier {cal_row.tier}",
                    str(cal_row.n),
                    f"{cal_row.mean_citations:.2f}",
                    f"{cal_row.median_citations:.2f}",
                )
            console.print(table)
            spearman = "—" if cal.spearman is None else f"{cal.spearman:+.3f}"
            console.print(f"Spearman(tier, 引用)：{spearman}")
            if not cal.enough:
                console.print(f"[yellow]结论[/] {cal.reason}")

    if impact:
        if not settings.db_path().exists():
            console.print("[yellow]warning[/] 数据库不存在，跳过影响分析")
        else:
            engine = create_engine(f"sqlite:///{settings.db_path()}", echo=False)
            with Session(engine) as session:
                imp = run_impact(session, settings.impact, limit=limit)
            table = Table(title="影响分析（被引增长）", title_justify="left")
            table.add_column("论文", max_width=44)
            table.add_column("快照")
            table.add_column("跨度")
            table.add_column("被引")
            table.add_column("每 30 天")
            table.add_column("判断", max_width=30)
            for imp_row in imp.rows:
                table.add_row(
                    imp_row.title[:44],
                    str(imp_row.snapshots),
                    f"{imp_row.span_days}d",
                    f"{imp_row.first_citations}→{imp_row.last_citations}",
                    f"{imp_row.velocity_per_30d:+.2f}",
                    imp_row.verdict,
                )
            console.print(table)
            if not imp.enough:
                console.print(f"[yellow]结论[/] {imp.reason}")
            else:
                console.print("★ 开创/跟风是**候选判断**，依据仅为引用增速，不作定论")
    raise typer.Exit(0)


@app.command()
def report(
    day: str | None = typer.Option(None, "--date", help="报告日期（默认今天，YYYY-MM-DD）"),
    out: str | None = typer.Option(None, "--out", help="输出目录（默认 data/reports）"),
) -> None:
    """生成当日报告快照：`data/reports/YYYY-MM-DD.md` + `.json`（★ 只读，不写库）。

    ★ 聚合「概览 / ★命中 Top10 / 运行状态」三块；「深度分析 / 新定级」是 L3 能力，恒空。
    """
    from ai_scholar.pipeline.report import build_daily_report, write_report
    from ai_scholar.utils.timeutil import today_local

    settings = load_settings()
    if day is None:
        today = today_local()
    else:
        try:
            today = date.fromisoformat(day)
        except ValueError:
            console.print(f"[red]日期格式错误[/]：{day}（应为 YYYY-MM-DD）")
            raise typer.Exit(2) from None

    if out:
        out_dir = Path(out).expanduser()
        if not out_dir.is_absolute():
            out_dir = project_root() / out_dir
    else:
        out_dir = settings.data_dir() / "reports"

    engine = create_engine(f"sqlite:///{settings.db_path()}", echo=False)
    watchlist = load_watchlist()
    with Session(engine) as session:
        daily = build_daily_report(
            session,
            today=today,
            watchlist=watchlist,
            data_dir=settings.data_dir(),
            timezone=settings.app.timezone,
        )

    outcome = write_report(daily, out_dir)

    table = Table(title=f"report · {daily.date}", title_justify="left")
    table.add_column("项")
    table.add_column("值")
    table.add_row("库内论文", str(daily.total_papers))
    table.add_row("今日新增", str(daily.new_today))
    table.add_row("★ 命中", str(len(daily.watchlist_hits)))
    table.add_row("今日 run", str(len(daily.runs)))
    table.add_row("md", str(outcome.md_path))
    table.add_row("json", str(outcome.json_path))
    console.print(table)
    if daily.degraded:
        for key, reason in daily.degraded.items():
            console.print(f"[yellow]warning[/] {key}：{reason}")
    raise typer.Exit(0)


def main() -> None:
    """包入口（`ai-scholar`）。配置错误统一收敛成一条提示，退出码 1。"""
    try:
        app()
    except ConfigError as exc:
        console.print(f"[red]配置错误[/]：{exc}")
        raise typer.Exit(1) from exc


if __name__ == "__main__":  # pragma: no cover
    main()
