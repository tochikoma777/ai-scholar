"""配置层测试。

★ 断言"**实际收到的值**"，不能只断言"配置加载成功"（本项目红线）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_scholar.config import RunMode, interpolate_env, load_settings, load_yaml, project_root


def test_default_yaml_uses_env_placeholders(tmp_path: Path) -> None:
    """仓库里那份 default.yaml 的环境开关必须是 `${ENV}` 占位符，不能是字面量。

    写成字面量会被 `Settings(**yaml)` 的 init 参数压掉环境变量（aipulse 踩过的坑）。
    """
    raw = (project_root() / "config" / "default.yaml").read_text(encoding="utf-8")
    for key in ("mode:", "api_key:", "base_url:"):
        line = next(x for x in raw.splitlines() if x.strip().startswith(key))
        assert "${" in line, f"{key} 必须是 ${{ENV}} 占位符，实际：{line.strip()}"


def test_env_overrides_yaml_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`AI_SCHOLAR_MODE=strong` 必须真的生效（断言实际枚举值，不是"加载成功"）。"""
    cfg = tmp_path / "default.yaml"
    cfg.write_text("mode: ${AI_SCHOLAR_MODE:-fast}\n", encoding="utf-8")
    monkeypatch.setenv("AI_SCHOLAR_MODE", "strong")
    assert load_settings(cfg).mode is RunMode.STRONG


def test_literal_yaml_defeats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """反例：yaml 写字面量时环境变量被**静默压掉**——这正是必须用占位符的原因。"""
    cfg = tmp_path / "default.yaml"
    cfg.write_text("mode: fast\n", encoding="utf-8")
    monkeypatch.setenv("AI_SCHOLAR_MODE", "strong")
    assert load_settings(cfg).mode is RunMode.FAST


def test_missing_api_key_degrades_to_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 api_key ⇒ effective_mode() 返回 off 且不抛异常（未配 key 不得报错退出）。"""
    cfg = tmp_path / "default.yaml"
    cfg.write_text("mode: strong\nllm:\n  api_key: ${AI_SCHOLAR_LLM__API_KEY}\n", encoding="utf-8")
    monkeypatch.delenv("AI_SCHOLAR_LLM__API_KEY", raising=False)
    settings = load_settings(cfg)
    assert settings.mode is RunMode.STRONG  # 声明档位保留
    assert settings.effective_mode() is RunMode.OFF  # 实际档位降级


def test_api_key_without_base_url_still_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只配 key 不配 base_url 也要降级（否则会在构造客户端时崩，aipulse 的真实坑）。"""
    cfg = tmp_path / "default.yaml"
    cfg.write_text(
        "mode: fast\nllm:\n  api_key: ${AI_SCHOLAR_LLM__API_KEY}\n"
        "  base_url: ${AI_SCHOLAR_LLM__BASE_URL}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_SCHOLAR_LLM__API_KEY", "sk-test")
    monkeypatch.delenv("AI_SCHOLAR_LLM__BASE_URL", raising=False)
    assert load_settings(cfg).effective_mode() is RunMode.OFF


def test_sources_config_loaded_from_repo() -> None:
    """仓库默认配置里 arXiv / HF 必须可达且限速正确（★ arXiv 约 1 req / 3s）。"""
    settings = load_settings()
    arxiv = settings.source("arxiv")
    assert arxiv is not None
    assert arxiv.rate_limit == "1/3s"
    assert arxiv.time_semantics == "discovery"  # 索引滞后 ⇒ 用 first_seen 过滤
    assert arxiv.categories() == ["cs.AI", "cs.CL", "cs.LG"]
    assert settings.source("hf_daily") is not None


def test_project_root_honors_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    assert project_root() == tmp_path.resolve()


def test_interpolate_env_default_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_SCHOLAR_X", "value")
    monkeypatch.delenv("AI_SCHOLAR_Y", raising=False)
    assert interpolate_env("${AI_SCHOLAR_X}") == "value"
    assert interpolate_env("${AI_SCHOLAR_Y}") == ""
    assert interpolate_env("${AI_SCHOLAR_Y:-fallback}") == "fallback"


def test_load_yaml_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_yaml(tmp_path / "nope.yaml") == {}


def test_load_yaml_non_mapping_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    from ai_scholar.utils.errors import ConfigError

    with pytest.raises(ConfigError):
        load_yaml(bad)


def test_repo_default_settings_load_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """仓库默认配置在无任何环境变量时也能加载（CI 无密钥全绿的前提）。"""
    for key in list(os.environ):
        if key.startswith("AI_SCHOLAR_"):
            monkeypatch.delenv(key, raising=False)
    settings = load_settings()
    assert settings.effective_mode() is RunMode.OFF
    assert settings.app.name == "ai-scholar"
