"""采集器的统一中间表示。

各源（arXiv / HF / 将来的 S2）都转成 `RawPaper`，下游（三级去重、upsert）
只认这个结构 —— 新增数据源不需要动去重逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field

SOURCE_ARXIV = "arxiv"
SOURCE_HF = "hf_papers"
SOURCE_S2 = "s2"


@dataclass
class RawPaper:
    """一个源给出的原始条目（未归一化）。

    ★ 本阶段**只有元数据**，`full_text` 之类字段不存在 —— 论文正文永不入库。
    """

    source: str
    title: str
    abstract: str
    authors: list[str] = field(default_factory=list)
    # 身份相关（一级去重用）
    arxiv_id: str | None = None  # ★ 原始 id，可能带版本号（v2）
    doi: str | None = None
    url: str | None = None
    venue: str | None = None
    published_at: str | None = None
    primary_category: str | None = None
    categories: list[str] = field(default_factory=list)
    affiliations: list[str] = field(default_factory=list)
    # ★ 观测值（不进 content_hash，只存）：HF 热度、引用数
    hf_score: int | None = None
    citation_count: int | None = None
    refs_count: int | None = None
