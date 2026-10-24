"""CLI 骨架测试。

★ `collect` / `export` / `list` 是**刻意占位**：断言它们抛 NotImplementedError，
但 `--help` 必须可见（不是漏实现）。
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from ai_scholar.cli import _healthcheck, app

runner = CliRunner()


def test_help_lists_five_commands() -> None:
    result = runner.invoke(app, ["--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    for name in ("init", "doctor", "collect", "export", "list"):
        assert name in result.output, f"{name} 未在 --help 中出现"


@pytest.mark.parametrize("name", ["init", "doctor", "collect", "export", "list"])
def test_every_command_has_help(name: str) -> None:
    """★ L0-C 收尾：五个子命令全部实现，不再有占位。"""
    result = runner.invoke(app, [name, "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "Usage" in result.output or "--help" in result.output


def test_init_creates_skeleton_and_local_watchlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "watchlist.yaml").write_bytes(
        (repo_root / "config" / "watchlist.yaml").read_bytes()
    )
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    for key in list(os.environ):
        if key.startswith("AI_SCHOLAR_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))

    result = runner.invoke(app, ["init"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    local = tmp_path / "config" / "watchlist.local.yaml"
    assert local.exists(), "init 必须自动生成个人 watchlist"
    assert local.read_text(encoding="utf-8") == (repo_root / "config" / "watchlist.yaml").read_text(
        encoding="utf-8"
    )
    assert (tmp_path / ".env").exists()
    assert (tmp_path / "data" / "reports").is_dir()
    assert (tmp_path / "data" / "signals").is_dir()


def test_init_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    for key in list(os.environ):
        if key.startswith("AI_SCHOLAR_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    assert runner.invoke(app, ["init"]).exit_code == 0
    (tmp_path / "config" / "watchlist.local.yaml").write_text("mine\n", encoding="utf-8")
    result = runner.invoke(app, ["init"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # ★ 不得覆盖用户已改过的文件
    assert (tmp_path / "config" / "watchlist.local.yaml").read_text(encoding="utf-8") == "mine\n"


def test_doctor_without_key_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 未配 key ⇒ 提示"自动降级 off"，退出码 0（不是 error）。"""
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    for key in list(os.environ):
        if key.startswith("AI_SCHOLAR_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))

    result = runner.invoke(app, ["doctor", "--skip-healthcheck"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "off" in result.output
    assert "warning" in result.output  # 缺 watchlist.local.yaml 是 warning，不是 error


def test_doctor_reports_watchlist_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "watchlist.yaml").write_bytes(
        (repo_root / "config" / "watchlist.yaml").read_bytes()
    )
    (tmp_path / "config" / "watchlist.local.yaml").write_text(
        "groups:\n  - name: mine\n    keywords: [a, b]\n", encoding="utf-8"
    )
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    result = runner.invoke(app, ["doctor", "--skip-healthcheck"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "5 组" in result.output  # 4 个示例组 + 1 个自己的组


# ── export / list 冒烟（真实建库 + 真实导出到 tmp vault）───────────────


def _seed_db(tmp_path: Path, titles: list[str]) -> None:
    import json

    from sqlalchemy import create_engine
    from sqlmodel import Session, SQLModel

    from ai_scholar.store.models import Paper

    db = tmp_path / "data" / "ai_scholar.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for i, title in enumerate(titles):
            session.add(
                Paper(
                    id=f"arXiv:2610.0000{i}",
                    source="arxiv",
                    primary_source="arxiv",
                    first_seen_at="2026-10-20",
                    published_at="2026-10-19T00:00:00Z",
                    title=title,
                    abstract="abstract",
                    authors_json=json.dumps(["John Doe"]),
                    arxiv_id=f"2610.0000{i}",
                    primary_category="cs.AI",
                    content_hash="h",
                    selected=True,  # ★ export 默认只导 selected
                )
            )
        session.commit()


def test_export_cli_writes_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    _seed_db(root, ["A multimodal paper: with colon", "Another paper"])
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(root))
    vault = tmp_path / "vault"  # ★ 必须在代码仓库之外
    result = runner.invoke(
        app, ["export", "--limit", "1", "--out", str(vault)], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    assert "导出 1 篇" in result.output
    assert (vault / "00-首页.md").exists()
    assert (vault / "90-元数据" / "生成报告.md").exists()


def test_export_cli_rejects_bad_since(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    result = runner.invoke(app, ["export", "--since", "not-a-date"])
    assert result.exit_code == 2


def _install_watchlist(tmp_path: Path, repo_root: Path) -> None:
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "watchlist.yaml").write_bytes(
        (repo_root / "config" / "watchlist.yaml").read_bytes()
    )


def test_list_cli_shows_star(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    """★ `list` 必须显示 watchlist 命中标记（不是只在脚注里出现 ★）。"""
    _seed_db(tmp_path, ["A multimodal paper about embodied agents", "Unrelated work"])
    _install_watchlist(tmp_path, repo_root)
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    result = runner.invoke(app, ["list", "--limit", "10"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if "arXiv:2610.00000" in line]
    assert lines and lines[0].lstrip("│ ").startswith("★")
    assert "显示 2 条" in result.output


def test_list_watchlist_only_is_display_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    """★ `--watchlist-only` 只做展示筛选，不改库（行数不变）。"""
    from sqlalchemy import create_engine
    from sqlmodel import Session, select

    from ai_scholar.store.models import Paper

    _seed_db(tmp_path, ["A multimodal paper about embodied agents", "Unrelated work"])
    _install_watchlist(tmp_path, repo_root)
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    result = runner.invoke(
        app, ["list", "--limit", "10", "--watchlist-only"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0
    assert "显示 1 条" in result.output

    engine = create_engine(f"sqlite:///{tmp_path / 'data' / 'ai_scholar.db'}")
    with Session(engine) as session:
        assert len(session.exec(select(Paper)).all()) == 2  # ★ 库没被改


# ── collect ─────────────────────────────────────────────────────────────


def test_collect_days_defaults_to_lookback_and_is_overridable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ `--days` 默认取 `sources.arxiv.lookback_days`；显式传入时覆盖配置。"""
    import ai_scholar.pipeline.collect as pc

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.yaml").write_text(
        "sources:\n  arxiv:\n    lookback_days: 4\n", encoding="utf-8"
    )
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))

    captured: dict[str, object] = {}

    async def fake(**kwargs: object) -> pc.CollectReport:
        captured.update(kwargs)
        return pc.CollectReport(run_id="x")

    monkeypatch.setattr(pc, "run_collect", fake)
    assert runner.invoke(app, ["collect"]).exit_code == 0
    assert captured["days"] == 4  # 来自配置
    assert runner.invoke(app, ["collect", "--days", "2"]).exit_code == 0
    assert captured["days"] == 2  # ★ 显式传入覆盖配置


def test_collect_unknown_source_exits_2() -> None:
    result = runner.invoke(app, ["collect", "--source", "bogus"], env={"COLUMNS": "200"})
    assert result.exit_code == 2


def test_collect_cli_reports_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """collect 打印 fetched/new/updated/deduped 四数 + first_seen 新增行数，退出码 0。"""
    import ai_scholar.pipeline.collect as pc

    report = pc.CollectReport(dry_run=False, run_id="abc123")
    report.outcomes = [pc.SourceOutcome(name="arxiv", fetched=10, new=8, updated=1, deduped=1)]
    report.first_seen_new = 8

    async def fake(**_kwargs: object) -> pc.CollectReport:
        return report

    monkeypatch.setattr(pc, "run_collect", fake)
    result = runner.invoke(app, ["collect", "--days", "2"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "fetched=10" in result.output
    assert "new=8" in result.output
    assert "first_seen.jsonl 新增 8 行" in result.output


def test_collect_cli_degraded_still_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 单源降级：仍退出码 0，并提示已写 runs.degraded_json。"""
    import ai_scholar.pipeline.collect as pc

    report = pc.CollectReport(dry_run=False, run_id="abc124")
    hf = pc.SourceOutcome(name="hf_daily", degraded="RuntimeError: ConnectTimeout", failed=True)
    report.outcomes = [pc.SourceOutcome(name="arxiv", fetched=5, new=5), hf]
    report.degraded = {"hf_daily": "RuntimeError: ConnectTimeout"}

    async def fake(**_kwargs: object) -> pc.CollectReport:
        return report

    monkeypatch.setattr(pc, "run_collect", fake)
    result = runner.invoke(app, ["collect"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "warning" in result.output
    assert "degraded_json" in result.output


# ── doctor 的 healthcheck 分支（覆盖非 skip 路径，提升 cli 覆盖率）──────────


@respx.mock
async def test_healthcheck_ok() -> None:
    respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(200, text="<feed/>")
    )
    result = await _healthcheck(
        "arxiv", "https://export.arxiv.org/api/query", {"search_query": "cat:cs.AI"}, "10/1s"
    )
    assert result.startswith("HTTP 200")


@respx.mock
async def test_healthcheck_unreachable() -> None:
    """不可达时返回 warning 文案，**不抛异常**（单源异常不得打断体检）。"""
    respx.get("https://huggingface.co/api/daily_papers").mock(
        side_effect=httpx.ConnectError("unreachable")
    )
    result = await _healthcheck(
        "hf_daily", "https://huggingface.co/api/daily_papers", {"date": "2026-10-20"}, "10/1s"
    )
    assert result.startswith("不可达")


def _doctor_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    for key in list(os.environ):
        if key.startswith("AI_SCHOLAR_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))


def test_doctor_healthcheck_all_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """健康检查全部可达 ⇒ 数据源行标 OK。用打桩的 asyncio.run 覆盖循环体（不碰真实网络）。"""
    _doctor_env(tmp_path, monkeypatch)
    monkeypatch.setattr("ai_scholar.cli.asyncio.run", lambda coro: "HTTP 200 · 123 B")
    result = runner.invoke(app, ["doctor"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "数据源 arxiv" in result.output
    assert "数据源 hf_daily" in result.output


def test_doctor_healthcheck_unreachable_is_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 数据源不可达 ⇒ warning（不是 error），退出码仍 0。"""
    _doctor_env(tmp_path, monkeypatch)
    monkeypatch.setattr("ai_scholar.cli.asyncio.run", lambda coro: "不可达（ConnectTimeout）")
    result = runner.invoke(app, ["doctor"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "warning" in result.output


# ── budget（阶段 0：预算演练，零网络）─────────────────────────────────────


def test_budget_cli_estimates_and_allows() -> None:
    """budget 演练：估算成本并放行（★ 不真正调 LLM）。"""
    result = runner.invoke(
        app,
        ["budget", "--input-tokens", "30000", "--max-tokens", "4000"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    assert "deepseek-v4-flash" in result.output
    assert "放行" in result.output


def test_budget_cli_blocks_when_daily_exhausted() -> None:
    """★ 闸门拦截是硬停但**退出码仍 0**（成本预算 §3.3，不是错误）。"""
    result = runner.invoke(
        app,
        ["budget", "--input-tokens", "30000", "--max-tokens", "4000", "--spent-daily", "2.0"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    assert "拦截" in result.output


def test_budget_cli_unknown_tier_exits_2() -> None:
    result = runner.invoke(
        app,
        ["budget", "--input-tokens", "1", "--max-tokens", "1", "--tier", "bogus"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
