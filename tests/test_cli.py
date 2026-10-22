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


@pytest.mark.parametrize("name", ["export", "list"])
def test_placeholder_commands_raise_not_implemented(name: str) -> None:
    result = runner.invoke(app, [name])
    assert isinstance(result.exception, NotImplementedError)


@pytest.mark.parametrize("name", ["collect", "export", "list"])
def test_placeholder_commands_have_help(name: str) -> None:
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


# ── collect ─────────────────────────────────────────────────────────────


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
