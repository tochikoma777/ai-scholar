"""L1-0：vault 迁出 + 双仓库防护。

★ vault 是独立 private 仓库；它同时是**全项目唯一不可再生、无备份的用户数据**。
这里钉死五条防护中可自动验证的部分。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_scholar.cli import app
from ai_scholar.config import load_settings

runner = CliRunner()


def test_config_paths_do_not_leak_username(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 防护 1：config 只准 `~/` 开头，不得出现 /home/ 或 /Users/。"""
    # 防止开发者本地 shell 的覆盖值让断言失真（CI 无此变量）
    monkeypatch.delenv("AI_SCHOLAR_EXPORT__OUT_DIR", raising=False)
    settings = load_settings()
    assert settings.export.out_dir.startswith("~/"), settings.export.out_dir
    assert "/home/" not in settings.export.out_dir
    assert "/Users/" not in settings.export.out_dir

    for path in (repo_root / "config").glob("*.yaml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("#"):
                continue  # 注释里允许出现反例说明
            assert "/home/" not in line, f"{path.name} 泄露了绝对家目录路径"
            assert "/Users/" not in line, f"{path.name} 泄露了绝对家目录路径"


def test_default_out_dir_is_not_example_vault(repo_root: Path) -> None:
    """★ 防护 3：默认导出路径不得与示例 vault 混淆。"""
    default = Path(load_settings().export.out_dir).expanduser().resolve()
    example = (repo_root / "example_vault").resolve()
    assert default != example
    assert default.name != "example_vault"


def test_ci_does_not_export_to_default_vault(repo_root: Path) -> None:
    """★ 防护 2：Actions 日志默认公开 —— 禁止在 CI 里用默认路径 export。"""
    workflow = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for line in workflow.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("run:") and "ai-scholar export" in stripped:
            assert "--out" in stripped, "CI 禁止用默认 vault 路径导出（会泄露真实路径）"


def _doctor_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(root))
    # ★ vault 必须在“代码仓库”之外，否则会被防护 3 判为危险路径
    monkeypatch.setenv("AI_SCHOLAR_EXPORT__OUT_DIR", str(tmp_path / "vault"))
    result = runner.invoke(app, ["doctor", "--skip-healthcheck"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    return result.output


def test_doctor_warns_when_vault_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = _doctor_output(tmp_path, monkeypatch)
    assert "vault" in output
    assert "不存在" in output


def test_doctor_warns_when_vault_is_not_a_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "vault").mkdir()
    output = _doctor_output(tmp_path, monkeypatch)
    assert "未初始化 git" in output
    assert "warning" in output


def test_doctor_warns_when_vault_has_no_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    subprocess.run(["git", "init", "-q", str(vault)], check=True)
    output = _doctor_output(tmp_path, monkeypatch)
    assert "无 git remote" in output
    assert "warning" in output


def test_doctor_ok_when_vault_has_private_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    subprocess.run(["git", "init", "-q", str(vault)], check=True)
    subprocess.run(
        ["git", "-C", str(vault), "remote", "add", "origin", "git@example.invalid:v.git"],
        check=True,
    )
    output = _doctor_output(tmp_path, monkeypatch)
    assert "remote: origin" in output
