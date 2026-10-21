"""watchlist 双文件测试（★ 本项目特有）。

覆盖：local 深合并覆盖 default、缺项回落、匹配语义、local **不得入仓**。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_scholar.config import load_watchlist, match_watchlist, watchlist_paths
from ai_scholar.models import WatchlistConfig

DEFAULT_YAML = """default_threshold: 0.9
groups:
  - name: multimodal
    weight: 1.0
    keywords: [multimodal, embodied]
  - name: ai-safety
    weight: 1.0
    keywords: [reward hacking, sandbagging]
"""

LOCAL_YAML = """default_threshold: 1.5
groups:
  - name: multimodal
    weight: 2.0
    keywords: [video understanding]
  - name: my-own-topic
    weight: 1.2
    keywords: [speculative decoding]
"""


def _write(config_dir: Path, default: str | None = None, local: str | None = None) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    if default is not None:
        (config_dir / "watchlist.yaml").write_text(default, encoding="utf-8")
    if local is not None:
        (config_dir / "watchlist.local.yaml").write_text(local, encoding="utf-8")


def test_local_overrides_same_group_wholesale(tmp_path: Path) -> None:
    """同名组整组覆盖：weight 与 keywords 一起换（半合并会让分数无法解释）。"""
    _write(tmp_path, DEFAULT_YAML, LOCAL_YAML)
    wl = load_watchlist(tmp_path)
    multimodal = wl.group("multimodal")
    assert multimodal is not None
    assert multimodal.weight == 2.0
    assert multimodal.keywords == ["video understanding"]


def test_local_missing_group_falls_back_to_default(tmp_path: Path) -> None:
    """local 没写的组必须完整回落 default（deep merge 的要点）。"""
    _write(tmp_path, DEFAULT_YAML, LOCAL_YAML)
    wl = load_watchlist(tmp_path)
    safety = wl.group("ai-safety")
    assert safety is not None
    assert safety.weight == 1.0
    assert safety.keywords == ["reward hacking", "sandbagging"]


def test_local_new_group_appended(tmp_path: Path) -> None:
    _write(tmp_path, DEFAULT_YAML, LOCAL_YAML)
    wl = load_watchlist(tmp_path)
    assert wl.group_names() == ["multimodal", "ai-safety", "my-own-topic"]
    # multimodal 被整组覆盖后只剩 1 个关键词：1 + 2(ai-safety) + 1(my-own) = 4
    assert wl.total_keywords() == 4


def test_threshold_override(tmp_path: Path) -> None:
    _write(tmp_path, DEFAULT_YAML, LOCAL_YAML)
    assert load_watchlist(tmp_path).default_threshold == 1.5
    _write(tmp_path, DEFAULT_YAML, "groups:\n  - name: x\n    keywords: [y]\n")
    assert load_watchlist(tmp_path).default_threshold == 0.9  # 未写则回落


def test_default_only(tmp_path: Path) -> None:
    _write(tmp_path, DEFAULT_YAML)
    wl = load_watchlist(tmp_path)
    assert wl.default_threshold == 0.9
    assert len(wl.groups) == 2


def test_both_missing_returns_empty(tmp_path: Path) -> None:
    assert load_watchlist(tmp_path) == WatchlistConfig()


def test_repo_default_watchlist_matches_expected_shape() -> None:
    """仓库里那份 watchlist.yaml 必须是中性示例且阈值 0.9（任一命中即触发）。"""
    wl = load_watchlist()
    assert wl.default_threshold == 0.9
    assert "multimodal" in wl.group_names()
    assert wl.total_keywords() >= 15


def test_match_is_word_boundary_and_case_insensitive() -> None:
    wl = WatchlistConfig.model_validate(
        {
            "default_threshold": 0.9,
            "groups": [{"name": "g", "weight": 1.0, "keywords": ["KV cache"]}],
        }
    )
    assert match_watchlist(wl, "Efficient KV cache eviction").matched is True
    assert match_watchlist(wl, "Serving with kv CACHE reuse").matched is True
    # word-boundary：嵌在单词内部不算命中（`multicache` 不是 `KV cache`）
    assert match_watchlist(wl, "multicache design").matched is False


def test_match_score_counts_group_once() -> None:
    """同组多次命中只计一次——分数只用于排序，不代表"命中了几次"。"""
    wl = WatchlistConfig.model_validate(
        {
            "default_threshold": 0.9,
            "groups": [
                {"name": "a", "weight": 1.0, "keywords": ["quantization", "distillation"]},
                {"name": "b", "weight": 0.5, "keywords": ["benchmark"]},
            ],
        }
    )
    hit = match_watchlist(wl, "Quantization and Distillation", "evaluated on a benchmark")
    assert hit.groups == ["a", "b"]
    assert hit.score == 1.5
    assert hit.matched is True


def test_match_below_threshold_does_not_trigger() -> None:
    wl = WatchlistConfig.model_validate(
        {
            "default_threshold": 2.0,
            "groups": [{"name": "b", "weight": 0.5, "keywords": ["benchmark"]}],
        }
    )
    hit = match_watchlist(wl, "A new benchmark")
    assert hit.score == 0.5
    assert hit.matched is False  # ★ 分只是排序权重，不到阈值就不触发


def test_local_file_is_gitignored(repo_root: Path) -> None:
    """★ `config/watchlist.local.yaml` 绝不能被 git 跟踪。"""
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=repo_root, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(reason=f"无法执行 git ls-files（非 git 环境）：{exc}")
    assert "config/watchlist.local.yaml" not in out.splitlines()


def test_gitignore_declares_local_watchlist(repo_root: Path) -> None:
    text = (repo_root / ".gitignore").read_text(encoding="utf-8")
    assert "config/watchlist.local.yaml" in text


def test_watchlist_paths() -> None:
    default_path, local_path = watchlist_paths()
    assert default_path.name == "watchlist.yaml"
    assert local_path.name == "watchlist.local.yaml"
