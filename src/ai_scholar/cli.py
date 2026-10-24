"""CLI 入口。

五个子命令：`init` / `doctor` / `collect` / `export` / `list`。
L0-B 实现 `collect`；L0-C 实现 `export` / `list`。
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

    shown = 0
    with Session(engine) as session:
        # ★ `list` 是"看库里有什么"，不受选集限制（export 才默认只导 selected）
        papers = select_papers(session, limit=limit, since=since_date, all_papers=True)
        for paper in papers:
            hit = match_watchlist(watchlist, paper.title, paper.abstract or "")
            if watchlist_only and not hit.matched:
                continue
            shown += 1
            table.add_row(
                "★" if hit.matched else "",
                paper.id,
                (paper.first_seen_at or "")[:10],
                paper.title[:60],
                str(paper.hf_score) if paper.hf_score is not None else "",
            )
    console.print(table)
    console.print(f"显示 {shown} 条（★ = watchlist 命中）")
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
