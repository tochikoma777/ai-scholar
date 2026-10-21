"""测试公共 fixture。

★ `load_settings` 带 `lru_cache`，测试里改动环境变量 / AI_SCHOLAR_ROOT 后必须清缓存，
否则会拿到上一次的值（"配置加载成功"而实际值不对，正是本项目的红线测试方式）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ai_scholar.config import load_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    load_settings.cache_clear()
    yield
    load_settings.cache_clear()


@pytest.fixture
def repo_root() -> Path:
    """真实仓库根目录（`src/ai_scholar/config.py` 的上两级）。"""
    return Path(__file__).resolve().parents[1]
