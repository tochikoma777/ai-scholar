"""L4：分项 score（★ 不合成单一分）。"""

from __future__ import annotations

from ai_scholar.config import load_watchlist
from ai_scholar.pipeline.score import score_many
from ai_scholar.store.models import Paper


def make_paper(
    pid: str, *, hf: int | None = None, citations: int | None = None, title: str = "test"
) -> Paper:
    return Paper(
        id=pid,
        source="arxiv",
        primary_source="arxiv",
        first_seen_at="2026-01-01",
        published_at="2025-12-01T00:00:00Z",
        title=title,
        abstract="A long enough abstract about KV cache and speculative decoding.",
        authors_json="[]",
        primary_category="cs.AI",
        content_hash=f"h-{pid}",
        hf_score=hf,
        citation_count=citations,
    )


def test_components_are_separate_and_missing_heat_is_none() -> None:
    papers = [
        make_paper("a", hf=100, citations=50),
        make_paper("b", hf=None, citations=None),
    ]
    scores = score_many(papers, load_watchlist())

    assert scores["a"].heat == 1.0
    assert scores["a"].cross_source == 1.0
    assert scores["b"].heat is None  # ★ 没有数据不是 0
    assert scores["b"].cross_source == round(1 / 3, 4)
    # ★ 红线 11：不存在"综合分"字段
    assert not hasattr(scores["a"], "total")


def test_topic_uses_watchlist_weights_not_a_gate() -> None:
    watchlist = load_watchlist()
    papers = [make_paper("a", title="KV cache and speculative decoding for agents")]
    score = score_many(papers, watchlist)["a"]

    assert 0.0 <= score.topic <= 1.0
    assert score.groups  # 命中组保留，便于解释
