"""测试公共 fixture。

★ `load_settings` 带 `lru_cache`，测试里改动环境变量 / AI_SCHOLAR_ROOT 后必须清缓存，
否则会拿到上一次的值（"配置加载成功"而实际值不对，正是本项目的红线测试方式）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from ai_scholar.config import load_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    load_settings.cache_clear()
    yield
    load_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离开发者本地环境：不许 `.env`、也不许 shell 里的 `AI_SCHOLAR_*` 渗进测试。

    `load_settings()` 会 `load_dotenv()`，而 python-dotenv 是按**调用者文件位置**向上找
    `.env`（不是按 cwd），所以只 `chdir` 不够 —— 必须直接把 `load_dotenv` 打桩掉。
    否则仓库根目录一旦有 `.env`（跑过 `ai-scholar init` 就会有），
    "缺 base_url 应降级 off"这类断言会**在本地失败、在 CI 通过**，测试不可复现。

    ★ 只打桩 `load_dotenv` 也不够：pydantic-settings 仍优先读 `os.environ`。
    开发者 shell 里 `export AI_SCHOLAR_MODE=fast` / 真实 key 后，同样两条断言会红
    （2026-10-28 实测：`test_collect` / `test_export` 期望 `off` 拿到 `fast`）。
    所以把 `AI_SCHOLAR_*` 一并清掉；测试内部需要的变量由 `monkeypatch.setenv` 自己设。
    """
    monkeypatch.setattr("ai_scholar.config.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("AI_SCHOLAR_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def repo_root() -> Path:
    """真实仓库根目录（`src/ai_scholar/config.py` 的上两级）。"""
    return Path(__file__).resolve().parents[1]
