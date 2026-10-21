"""核心数据模型（L0-A：只定义本阶段真正用到的那些）。

★ 不要为"将来可能用到"提前建模。后续阶段（L0-B 的 DB schema / 采集器）
会在此基础上扩字段，而不是现在就铺满。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Availability = Literal["always", "actions_only"]
TimeSemantics = Literal["event", "state", "discovery"]


class WatchlistGroup(BaseModel):
    """一个兴趣方向。

    - `weight` 是**排序权重**（★ 不是门槛）：命中得分 = Σ(命中组权重)，
      同组多次命中只计一次。
    - 关键词匹配：title 或 abstract，word-boundary，大小写不敏感。
    """

    name: str
    weight: float = 1.0
    keywords: list[str] = Field(default_factory=list)


class WatchlistConfig(BaseModel):
    """watchlist 双文件合并后的结果。

    `default_threshold` 默认 0.9 —— 语义是"任一命中即触发"（见任务卡 2.4）：
    命中【布尔】决定是否进入系统，命中【分】只用于排序。
    """

    model_config = ConfigDict(extra="ignore")

    default_threshold: float = 0.9
    groups: list[WatchlistGroup] = Field(default_factory=list)

    def total_keywords(self) -> int:
        return sum(len(g.keywords) for g in self.groups)

    def group_names(self) -> list[str]:
        return [g.name for g in self.groups]

    def group(self, name: str) -> WatchlistGroup | None:
        for g in self.groups:
            if g.name == name:
                return g
        return None


class WatchlistHit(BaseModel):
    """一次匹配的结果。

    `matched` 决定**是否触发**（分数 ≥ threshold）；`score` 只用于排序。
    """

    matched: bool = False
    score: float = 0.0
    groups: list[str] = Field(default_factory=list)


class SourceProbeResult(BaseModel):
    """数据源探测结果（`scripts/probe_sources.py` 产出，写进 `docs/数据源手册.md`）。"""

    name: str
    endpoint: str
    request_url: str
    status_code: int | None = None
    response_bytes: int = 0
    item_count: int | None = None
    availability: Availability = "always"
    availability_reason: str = ""
    auth_required: bool = False
    rate_limit: str = ""
    content_type: str = ""
    fields: list[str] = Field(default_factory=list)
    sample_excerpt: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


def normalize_title(title: str) -> str:
    """标题归一化（去重键用）：小写、压缩空白、去首尾标点。

    ★ 只做"跨源同一篇能对上"所需的最小归一，不做模糊匹配——
    模糊去重误合两篇不同论文的代价远高于漏合。
    """
    text = re.sub(r"\s+", " ", title or "").strip().lower()
    return text.strip(" .,;:!?-–—")


def arxiv_id_from_entry(entry_id: str) -> str:
    """从 arXiv Atom 的 `<id>` 抽短 id（`http://arxiv.org/abs/2501.01234v2` → `2501.01234v2`）。"""
    return entry_id.rsplit("/abs/", 1)[-1] if "/abs/" in entry_id else entry_id
