"""CLI 骨架测试。

★ `collect` / `export` / `list` 是**刻意占位**：断言它们抛 NotImplementedError，
但 `--help` 必须可见（不是漏实现）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_scholar.cli import app

runner = CliRunner()


def test_help_lists_five_commands() -> None:
    result = runner.invoke(app, ["--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    for name in ("init", "doctor", "collect", "export", "list"):
        assert name in result.output, f"{name} 未在 --help 中出现"


@pytest.mark.parametrize("name", ["collect", "export", "list"])
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
