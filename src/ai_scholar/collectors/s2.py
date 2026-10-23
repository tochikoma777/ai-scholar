"""Semantic Scholar 采集器（★ 全程 `paper/batch`，禁止逐篇 GET）。

真实响应结构（2026-10-23 实测，摘录见 `docs/数据源手册.md`）：

```json
[{"paperId": "c1f7401a…", "externalIds": {"ArXiv": "2610.30122", "CorpusId": 293856663},
  "title": "A Quasipolynomial Algorithm…", "year": 2026, "referenceCount": 43,
  "references": [{"paperId": "b218d781…",
                  "externalIds": {"ArXiv": "2610.22149", "CorpusId": 293003685}}]},
 null]
```

★ 两个易错点：
1. **未命中的 id 返回 `null`**（位置仍然对齐），不是跳过 —— 解析必须保留位置。
2. `fields` 走 **URL query**，不是 body；`x-api-key` 大小写敏感，**无 key 则不发该头**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ai_scholar.collectors.base import SOURCE_S2
from ai_scholar.utils.errors import SourceError
from ai_scholar.utils.http import HttpClient

DEFAULT_FIELDS = (
    "paperId,externalIds,title,year,venue,publicationVenue,"
    "citationCount,referenceCount,references.paperId,references.externalIds"
)
DEFAULT_BATCH_SIZE = 500
MIN_BATCH_SIZE = 10
DEFAULT_RATE_LIMIT = "1/1s"
# ★ 响应体过大 / ids 过多时 S2 会拒绝：降半重试（500 → 250 → 125 …）
OVERSIZE_STATUS = frozenset({400, 413, 414, 431})


@dataclass
class S2Reference:
    """一条被引记录（只有 id 信息 —— 我们**不回采**被引论文的元数据）。"""

    paper_id: str | None = None
    arxiv_id: str | None = None
    doi: str | None = None


@dataclass
class S2Record:
    """S2 返回的一条论文记录。"""

    paper_id: str | None = None
    arxiv_id: str | None = None
    doi: str | None = None
    title: str | None = None
    year: int | None = None
    venue: str | None = None
    citation_count: int | None = None
    reference_count: int | None = None
    references: list[S2Reference] = field(default_factory=list)


def _external_ids(raw: Any) -> tuple[str | None, str | None]:
    """从 `externalIds` 取 `(arxiv_id, doi)`。"""
    if not isinstance(raw, dict):
        return None, None
    arxiv = raw.get("ArXiv") or raw.get("arXiv")
    doi = raw.get("DOI")
    return (str(arxiv) if arxiv else None), (str(doi) if doi else None)


def parse_record(raw: Any) -> S2Record | None:
    """解析单条记录。**未命中返回 None**（S2 用 null 占位，位置必须对齐）。"""
    if not isinstance(raw, dict):
        return None
    arxiv_id, doi = _external_ids(raw.get("externalIds"))
    refs: list[S2Reference] = []
    for item in raw.get("references") or []:
        if not isinstance(item, dict):
            continue
        ref_arxiv, ref_doi = _external_ids(item.get("externalIds"))
        refs.append(
            S2Reference(
                paper_id=(str(item.get("paperId")) if item.get("paperId") else None),
                arxiv_id=ref_arxiv,
                doi=ref_doi,
            )
        )
    return S2Record(
        paper_id=(str(raw.get("paperId")) if raw.get("paperId") else None),
        arxiv_id=arxiv_id,
        doi=doi,
        title=(str(raw.get("title")) if raw.get("title") else None),
        year=(int(raw["year"]) if isinstance(raw.get("year"), int) else None),
        venue=(str(raw.get("venue")) if raw.get("venue") else None),
        citation_count=(
            int(raw["citationCount"]) if isinstance(raw.get("citationCount"), int) else None
        ),
        reference_count=(
            int(raw["referenceCount"]) if isinstance(raw.get("referenceCount"), int) else None
        ),
        references=refs,
    )


def parse_batch(payload: Any) -> list[S2Record | None]:
    """解析 batch 响应，**保持与请求 ids 的位置对齐**。"""
    if not isinstance(payload, list):
        raise ValueError(f"S2 batch 响应不是数组：{type(payload).__name__}")
    return [parse_record(item) for item in payload]


async def _post_batch(
    client: HttpClient,
    ids: list[str],
    *,
    endpoint: str,
    api_key: str,
    fields: str,
    rate_limit: str,
) -> Any:
    """发一次 batch 请求（★ 无 key 时不发 `x-api-key` 头）。"""
    headers = {"x-api-key": api_key} if api_key else None
    return await client.post(
        endpoint,
        params={"fields": fields},
        json_body={"ids": ids},
        headers=headers,
        source=SOURCE_S2,
        rate_limit=rate_limit,
        timeout=None,
    )


async def fetch_s2_papers(
    client: HttpClient,
    ids: list[str],
    *,
    endpoint: str,
    api_key: str = "",
    fields: str = DEFAULT_FIELDS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    rate_limit: str = DEFAULT_RATE_LIMIT,
) -> list[S2Record | None]:
    """按 `batch_size` 分批请求，返回与输入 ids **位置对齐**的记录。

    ★ 单响应 ≤10MB / 单篇 references ≤9999 条：超限时 S2 会拒绝，
      这里**自动降半重试**（500 → 250 → 125 …），降到 `MIN_BATCH_SIZE` 仍失败才抛异常。
    """
    if not ids:
        return []

    size = max(MIN_BATCH_SIZE, min(batch_size, len(ids)))
    out: list[S2Record | None] = []
    pos = 0
    while pos < len(ids):
        take = min(size, len(ids) - pos)
        chunk = ids[pos : pos + take]
        response = await _post_batch(
            client,
            chunk,
            endpoint=endpoint,
            api_key=api_key,
            fields=fields,
            rate_limit=rate_limit,
        )
        if response.status_code == 200:
            out.extend(parse_batch(response.json()))
            pos += take
            continue
        if response.status_code in OVERSIZE_STATUS and take > MIN_BATCH_SIZE:
            size = max(MIN_BATCH_SIZE, take // 2)  # ★ 降半重试
            continue
        raise SourceError(SOURCE_S2, f"HTTP {response.status_code}（ids={take}）")
    return out
