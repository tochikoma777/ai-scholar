"""CLI 入口。

五个子命令：`init` / `doctor` / `collect` / `export` / `list`。
★ `collect` / `export` / `list` 在 L0-A 是**刻意占位**（`NotImplementedError`），
它们的 `--help` 必须可见，实现留给后续阶段（L0-B 起）。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from ai_scholar import __version__
from ai_scholar.config import (
    load_settings,
    load_watchlist,
    project_root,
    watchlist_paths,
)
from ai_scholar.utils.errors import ConfigError
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
        table.add_row("数据目录", "[green]OK[/]", f"{data_dir}{free_text}")
    else:
        table.add_row(
            "数据目录",
            "[yellow]缺失[/]",
            f"{data_dir} 不存在；运行 `ai-scholar init` 创建{free_text}",
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
    since: str = typer.Option("24h", "--since", help="回溯窗口，如 24h / 7d"),
) -> None:
    """采集：从 arXiv / HF Daily Papers 拉取新论文并去重入库（L0-B 实现）。"""
    raise NotImplementedError(f"collect(since={since}) 将在 L0-B（DB schema + 采集器）实现")


@app.command()
def export(
    out: Path = typer.Option(Path("vault"), "--out", help="Obsidian vault 输出目录"),
) -> None:
    """导出：生成自包含 Markdown 的 Obsidian 知识库（L0-C 及以后实现）。"""
    raise NotImplementedError(f"export(out={out}) 将在导出层实现")


@app.command("list")
def list_papers(
    limit: int = typer.Option(20, "--limit", "-n", help="显示条数"),
) -> None:
    """列出：库内论文条目（L0-B 起可用）。

    ★ 函数名不能叫 `list`——那会遮蔽内建的 `list` 类型（mypy strict 直接报错）。
    """
    raise NotImplementedError(f"list(limit={limit}) 将在 L0-B 实现")


def main() -> None:
    """包入口（`ai-scholar`）。配置错误统一收敛成一条提示，退出码 1。"""
    try:
        app()
    except ConfigError as exc:
        console.print(f"[red]配置错误[/]：{exc}")
        raise typer.Exit(1) from exc


if __name__ == "__main__":  # pragma: no cover
    main()
