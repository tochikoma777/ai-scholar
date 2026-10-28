"""Exporter 抽象（C1.3 硬要求）。

★ 即使目前只有 Obsidian 一个实现，这一层也必须留 —— 它是 DB → 文件的**唯一出口**，
Markdown 是 DB 的"物化视图"（单向生成，设计方案 §3.1）。

★ 只留这一层最小面：`render` / `write` / `name`。
**不要为"将来"多写抽象** —— 不做 Registry、不做插件发现。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_scholar.store.models import Paper


@dataclass
class LinkRef:
    """一条关联（渲染用；数据来自 `paper_links`）。"""

    paper_id: str
    title: str
    url: str | None = None
    link_type: str = "references"
    weight: float | None = None
    evidence: str | None = None  # ★ 可解释性：为什么关联
    interpretation: str | None = None  # ★ L3a 关联解读（强相似边的 LLM 一句解读）
    published_at: str | None = None  # 无笔记时的排序依据


@dataclass(frozen=True)
class LinkEdge:
    """一条**有向**关联（`links.json` 用；`LinkRef` 是渲染用的"对端"视角）。

    ★ `links.json` 必须有 `src_id` / `dst_id`：只有单向会让人分不清"A 引用了 B"
      还是"B 引用了 A"（与笔记里出边/入边分开渲染是同一个道理）。
    """

    src_id: str
    dst_id: str
    link_type: str
    weight: float | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class TopicEntry:
    """主题 MOC 里的一行（★ `note_name` 为 None ⇒ **不做 wikilink**，避免死链）。"""

    id: str
    title: str
    first_seen: str | None = None
    tier: int | None = None
    note_name: str | None = None


@dataclass
class ExportContext:
    """渲染一篇笔记所需的一切（不让它自己去查库 / 读配置）。"""

    llm_mode: str = "off"
    vault_dir: Path = Path("vault")
    # ★ 出边（我引用了谁）与入边（谁引用了我）**分开传** —— 方向错了整个关联图就错了
    out_links: list[LinkRef] = field(default_factory=list)
    in_links: list[LinkRef] = field(default_factory=list)
    # ★ 对称关联（L1-C）：语义相似 / 文献耦合（边只存一条，双向渲染）
    similar: list[LinkRef] = field(default_factory=list)
    coupled: list[LinkRef] = field(default_factory=list)
    # ★ 双轨渲染用：`paper_id -> 笔记名（不含 .md）`；不在其中的目标**不建空笔记**
    note_index: dict[str, str] = field(default_factory=dict)
    # ★ watchlist 命中组名（只打★与排序，**不决定是否导出**）
    watchlist_groups: list[str] = field(default_factory=list)
    degraded: dict[str, str] = field(default_factory=dict)
    # ★ L7-8：客观信号分（**分项**：heat / cross_source / topic，★ 红线 11：永不合成总分）
    score: Any | None = None


@dataclass
class ExportDoc:
    """一次写入的产物：路径 + 完整文本。"""

    path: Path
    content: str


class Exporter(ABC):
    """导出器抽象：`render` 出文本，`write` 落盘（原子写）。"""

    name: str = "base"

    @abstractmethod
    def render(self, paper: Paper, context: ExportContext) -> str:
        """把一篇论文渲染成完整文件文本（含 frontmatter + 标记块 + 用户区）。"""
        raise NotImplementedError

    def write(self, doc: ExportDoc) -> Path:
        """★ 原子写入：临时文件 + `os.replace` rename。

        不这么做的话，Obsidian 同步工具会读到半截文件（执行手册 §2.3）。
        """
        path = Path(doc.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(doc.content, encoding="utf-8")
        os.replace(tmp, path)
        return path
