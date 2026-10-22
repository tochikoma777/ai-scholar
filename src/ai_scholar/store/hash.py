"""身份归一化与 `content_hash`。

★ 本模块是本阶段**最容易做错**的地方（设计方案 §3.2.2 / 执行手册红线 14）：

```
content_hash = sha256( normalize(title)
                     + normalize(abstract)
                     + normalize_authors(authors)
                     + normalize(venue) )
```

| # | 硬约束 | 违反后果 |
|---|---|---|
| 1 | 必须排除引用数 / hf_score / 热度 | hash 每天变 ⇒ 重复入库、first_seen 被覆盖 |
| 2 | 作者先归一化（小写、去标点、姓+名首字母） | J. Doe 与 John Doe 两个 hash ⇒ 跨源假重复 |
| 3 | 标题归一化（小写、合并空白、去首尾标点） | 同上 |

> 关键区分：**不进 hash ≠ 不存储**。引用数照存 `papers.citation_count`，
> 时序快照存 `signals` 按天一行。类比：身份证号不随体重变。
"""

from __future__ import annotations

import hashlib
import re

_ARXIV_VERSION = re.compile(r"v\d+$", re.IGNORECASE)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
_EDGE_PUNCT = " .,;:!?-–—'\"“”‘’"
_SEP = "\x1f"  # 字段分隔符，避免拼接歧义


def normalize_text(text: str | None) -> str:
    """小写 + 合并空白 + 去首尾标点（标题 / venue 用）。"""
    if not text:
        return ""
    cleaned = _WS.sub(" ", str(text)).strip().lower()
    return cleaned.strip(_EDGE_PUNCT)


def normalize_abstract(text: str | None) -> str:
    """摘要归一化：小写 + 合并空白（不去首尾标点，摘要本身无边界问题）。"""
    if not text:
        return ""
    return _WS.sub(" ", str(text)).strip().lower()


def _initials(tokens: list[str]) -> str:
    return "".join(t[0] for t in tokens if t)


def normalize_author(name: str | None) -> str:
    """作者归一化：**小写、去标点、`姓 + 名首字母`**。

    `John Doe` / `J. Doe` / `J Doe` / `DOE, John` ⇒ 都得到 `doe j`
    （跨源同一作者必须同 hash，否则会算出两个 hash ⇒ 假重复）。

    ★ 逗号格式（`Doe, John`）是**姓在前**，与 `John Doe`（姓在后）相反 ——
    不特殊处理就会把名当姓，跨源匹配直接失效。
    """
    if not name:
        return ""
    text = str(name).lower()
    if "," in text:
        head, _sep, tail = text.partition(",")
        surname = [t for t in _WS.split(_PUNCT.sub(" ", head)) if t]
        given = [t for t in _WS.split(_PUNCT.sub(" ", tail)) if t]
        if not surname:
            return ""
        return f"{surname[-1]} {_initials(given)}".strip()
    tokens = [t for t in _WS.split(_PUNCT.sub(" ", text)) if t]
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0]
    return f"{tokens[-1]} {_initials(tokens[:-1])}".strip()


def normalize_authors(authors: list[str] | None) -> str:
    """作者列表归一化：**逐个归一化后排序再拼接**（跨源作者顺序可能不一致）。"""
    if not authors:
        return ""
    normalized = sorted({normalize_author(a) for a in authors if a})
    return "|".join(normalized)


def normalize_venue(venue: str | None) -> str:
    return normalize_text(venue)


def content_hash(
    title: str | None,
    abstract: str | None = None,
    authors: list[str] | None = None,
    venue: str | None = None,
) -> str:
    """★ 内容身份 hash：只由 title + abstract + authors + venue 计算。

    **绝不包含** citation_count / hf_score / score / 任何热度或观测值。
    """
    payload = _SEP.join(
        [
            normalize_text(title),
            normalize_abstract(abstract),
            normalize_authors(authors),
            normalize_venue(venue),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── 主键归一化（三级去重之一级：身份）─────────────────────────────────────


def canonical_arxiv_id(raw_id: str | None) -> str:
    """arXiv id 归一化：**剥离版本号**（`2610.22350v2` → `2610.22350`）。"""
    if not raw_id:
        return ""
    text = str(raw_id).strip()
    if "/abs/" in text:
        text = text.rsplit("/abs/", 1)[-1]
    return _ARXIV_VERSION.sub("", text)


def normalize_doi(doi: str | None) -> str:
    """DOI 归一化：去空白 + 前缀统一小写。"""
    if not doi:
        return ""
    return str(doi).strip().lower()


def paper_id_for_arxiv(arxiv_id: str | None) -> str:
    """`arXiv:{剥离版本号后的 id}`。"""
    canonical = canonical_arxiv_id(arxiv_id)
    return f"arXiv:{canonical}" if canonical else ""


def paper_id_for_doi(doi: str | None) -> str:
    """`DOI:{小写 doi}`。"""
    normalized = normalize_doi(doi)
    return f"DOI:{normalized}" if normalized else ""
