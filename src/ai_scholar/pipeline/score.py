"""L4 客观信号分：**分项展示，绝不合成单一综合分**（红线 11）。

三个分量各自可解释：

| 分量 | 含义 | 依据 |
|---|---|---|
| `heat` | 热度 | HF 票数 + S2 被引（对数归一化，缺一取有） |
| `cross_source` | 跨源确认 | arXiv 基础 +1、HF 有票 +1、S2 有引用 +1，除以 3 |
| `topic` | 主题相关 | watchlist 命中分 / 全部组权重之和 |

★ 不提供 `total` 字段，也不在代码里做加权求和 —— 由人自己权衡（§7 / 执行手册红线 11）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ai_scholar.config import match_watchlist
from ai_scholar.models import WatchlistConfig
from ai_scholar.store.models import Paper


@dataclass(frozen=True)
class ScoreBreakdown:
    paper_id: str
    heat: float | None
    cross_source: float
    topic: float
    groups: tuple[str, ...] = ()
    basis: dict[str, Any] = field(default_factory=dict)


def _normalizer(values: list[int]) -> int:
    positive = [value for value in values if value > 0]
    return max(positive) if positive else 0


def _heat(
    paper: Paper, *, max_hf: int, max_citations: int
) -> tuple[float | None, dict[str, float | None]]:
    """热度 = 可用分量的对数归一化均值；两个都没有 ⇒ None（不假装是 0）。"""
    parts: list[float] = []
    if max_hf > 0 and paper.hf_score is not None and paper.hf_score > 0:
        parts.append(math.log1p(paper.hf_score) / math.log1p(max_hf))
    if max_citations > 0 and paper.citation_count is not None and paper.citation_count > 0:
        parts.append(math.log1p(paper.citation_count) / math.log1p(max_citations))
    if not parts:
        return None, {"hf_score": paper.hf_score, "citation_count": paper.citation_count}
    return round(sum(parts) / len(parts), 4), {
        "hf_score": paper.hf_score,
        "citation_count": paper.citation_count,
    }


def score_many(papers: list[Paper], watchlist: WatchlistConfig) -> dict[str, ScoreBreakdown]:
    """对一批论文计算分项分（★ 归一化在同一批内完成，跨批不可比）。"""
    max_hf = _normalizer([p.hf_score or 0 for p in papers])
    max_citations = _normalizer([p.citation_count or 0 for p in papers])
    total_weight = sum(group.weight for group in watchlist.groups) or 1.0

    scores: dict[str, ScoreBreakdown] = {}
    for paper in papers:
        heat, heat_basis = _heat(paper, max_hf=max_hf, max_citations=max_citations)
        hit = match_watchlist(watchlist, paper.title, paper.abstract or "")
        cross = (1 + int(paper.hf_score is not None) + int(paper.citation_count is not None)) / 3.0
        scores[paper.id] = ScoreBreakdown(
            paper_id=paper.id,
            heat=heat,
            cross_source=round(min(1.0, cross), 4),
            topic=round(min(1.0, hit.score / total_weight), 4),
            groups=tuple(hit.groups),
            basis={
                "watchlist_score": hit.score,
                "watchlist_threshold": watchlist.default_threshold,
                **heat_basis,
            },
        )
    return scores


def fmt(value: float | None) -> str:
    """表格展示：缺失显示 `—`，不把"没数据"写成 0。"""
    return "—" if value is None else f"{value:.2f}"
