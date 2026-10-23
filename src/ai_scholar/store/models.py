"""六张表的 SQLModel 定义。

★ 定位：`data/ai_scholar.db` 是**可丢弃缓存**（元数据可从源重采）。
真正不可再生的是 `data/first_seen.jsonl`（见 `store/first_seen.py`）。

时间字段统一用 **TEXT（ISO-8601 UTC）**，由 `timeutil.utcnow()` 生成 ——
代码内禁止不带时区的取当前时间调用。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from ai_scholar.utils.timeutil import utcnow


def _now() -> str:
    return utcnow().isoformat(timespec="seconds")


# ── 常量（★ 方向语义与状态机集中定义，避免各处写字面量）─────────────────
# `paper_links(src, dst, references)` 的语义固定为：**src 引用了 dst**
LINK_TYPE_REFERENCES = "references"
EVIDENCE_S2_REFERENCES = "s2_references"

# 补齐队列状态（★ 复用 papers 表字段，不新建队列表）
ENRICH_PENDING = "pending"
ENRICH_DONE = "done"
ENRICH_STALE = "stale"

# 采样状态机（§3.7.2；★ 持久状态，不是每日重算）
SIGNAL_PENDING = "pending"  # 预筛通过 & 未定级
SIGNAL_READY = "ready"  # 快路径：进入时 published_at ≥ 180 天，跳过 pending 等 L3 定级
SIGNAL_TRACKED = "tracked"  # 已定级 / 已深度分析
SIGNAL_DORMANT = "dormant"  # 不常规观测（可单点复活一次）
SIGNAL_SAMPLE = "sample"  # 预筛淘汰的留样集（仅校准）
SIGNAL_REJECTED = "rejected"  # 预筛淘汰且未抽中留样 ⇒ 不跟踪

# 观测来源
SIGNAL_SOURCE_S2 = "s2"


class Paper(SQLModel, table=True):
    """① 论文主表。

    ★ `content_hash` 只由 title + abstract + authors + venue 计算，
    **必须排除 citation_count / hf_score / 任何热度**（每天在变的量进 hash
    ⇒ 每天重复入库 ⇒ 成本线性增长）。
    """

    __tablename__ = "papers"
    __table_args__ = (
        Index("idx_papers_first_seen", "first_seen_at"),
        Index("idx_papers_hash", "content_hash"),
    )

    # 归一化主键：`arXiv:{id}`（剥离版本号）或 `DOI:{doi}`
    id: str = Field(primary_key=True)
    source: str  # arxiv | hf_papers | s2
    primary_source: str
    # ★ 不可再生；真源是 data/first_seen.jsonl，db 里只是缓存
    first_seen_at: str
    published_at: str | None = None  # event 时间
    title: str
    abstract: str | None = None
    authors_json: str | None = None  # JSON 数组
    affiliations_json: str | None = None
    venue: str | None = None
    url: str | None = None
    arxiv_id: str | None = None  # ★ 剥离版本号后的规范 id（带版本号的进 aliases）
    doi: str | None = None
    primary_category: str | None = None
    categories_json: str | None = None
    # ★ 观测值（不进 content_hash，只存）：HF 热度信号
    hf_score: int | None = None
    citation_count: int | None = None  # 观测值，不进 hash
    refs_count: int | None = None
    # ★ 导出后回填的 vault 笔记相对路径（NULL = 无笔记）。
    #   双轨渲染靠它判断"目标有没有笔记"——扫目录每次 O(n) 且文件名含短 hash，不可靠。
    note_path: str | None = None
    # ★ 补齐队列（复用本表，不新建队列表）
    enrich_state: str = ENRICH_PENDING
    enrich_attempts: int = 0
    enrich_last_at: str | None = None
    enrich_error: str | None = None
    # ★ 采样状态机（§3.7.2）：状态**持久化**，不是每日重算。
    #   NULL = 尚未预筛（新入库 / 迁移前的历史行），下一次 collect 或 signals 补跑。
    signal_state: str | None = None
    signal_next_at: str | None = None  # 下次该观测的日期（YYYY-MM-DD）
    signal_last_at: str | None = None
    signal_revived: bool = False  # dormant 的单点复活是否已用掉（★ 最多一次）
    prescreen_reason: str | None = None  # 预筛淘汰原因（NULL = 通过）
    # ★ 选集：export 默认**只导出** selected=1（§5.6 v1.7，防每天造 20 篇新笔记）
    selected: bool = False
    content_hash: str
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class PaperAlias(SQLModel, table=True):
    """② 三级去重之三：跨版本归一（预印本 ↔ 正式版）。

    ★ 别名**不产生新条目**，只把 alias 指到 canonical_id。
    """

    __tablename__ = "paper_aliases"

    alias: str = Field(primary_key=True)  # DOI:{doi}、带版本号的 arXiv id、s2_corpus
    canonical_id: str = Field(foreign_key="papers.id")
    alias_kind: str  # doi | arxiv_version | s2_corpus
    created_at: str = Field(default_factory=_now)


class PaperLink(SQLModel, table=True):
    """③ 关联边。本阶段只建表，不写数据（L1 由 S2 填充）。"""

    __tablename__ = "paper_links"
    # ★ 反向索引：按 dst 回写（关联必须是双向可见的）
    __table_args__ = (Index("idx_links_dst", "dst_id"),)

    src_id: str = Field(primary_key=True)
    dst_id: str = Field(primary_key=True)
    link_type: str = Field(primary_key=True)  # references | similarity | entity_cooccur | temporal
    weight: float | None = None
    evidence: str | None = None  # ★ 可解释性：为什么关联
    created_at: str = Field(default_factory=_now)


class Signal(SQLModel, table=True):
    """④ 时序快照。本阶段只建表，不写数据（L1 填充）。"""

    __tablename__ = "signals"

    paper_id: str = Field(primary_key=True)
    observed_at: str = Field(primary_key=True)
    citation_count: int | None = None
    reference_count: int | None = None
    source: str


class Run(SQLModel, table=True):
    """⑤ 运行状态。★ 降级状态必须显式暴露在 degraded_json。"""

    __tablename__ = "runs"

    run_id: str = Field(primary_key=True)
    started_at: str
    finished_at: str | None = None
    mode: str  # off | fast | strong
    source: str | None = None  # 单源一行时为源名
    fetched: int = 0
    new: int = 0
    updated: int = 0
    deduped: int = 0
    degraded_json: str | None = None


class LLMUsage(SQLModel, table=True):
    """⑥ LLM 用量。★ off 档必须 0 行。"""

    __tablename__ = "llm_usage"

    id: int | None = Field(default=None, primary_key=True)
    run_id: str | None = None
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    purpose: str | None = None  # prescreen | grading | deep_analysis | enhance
    created_at: str = Field(default_factory=_now)


def now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（供调用方记录时间字段）。"""
    return _now()


def parse_iso(text: str) -> datetime | None:
    """解析 ISO 时间串（容错返回 None）。"""
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
