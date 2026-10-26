"""arXiv 采集器。

约束（任务卡 §2.4）：
- ★ **https**（http 会 301）
- 限速约 1 req / 3s（走 `HttpClient` 的令牌桶）
- 单页最多 200，自动翻页
- 按 `submittedDate` 精确框定某一天（不是"取最近 N 条"）
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from datetime import date

from ai_scholar.collectors.base import SOURCE_ARXIV, RawPaper
from ai_scholar.store.hash import canonical_arxiv_id
from ai_scholar.utils.http import HttpClient

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
PAGE_SIZE = 200  # arXiv API 单次返回上限


def submitted_query(categories: list[str], day: date) -> str:
    """`(cat:cs.AI OR ...) AND submittedDate:[YYYYMMDD0000 TO YYYYMMDD2359]`。"""
    cats = " OR ".join(f"cat:{c}" for c in categories)
    stamp = day.strftime("%Y%m%d")
    return f"({cats}) AND submittedDate:[{stamp}0000 TO {stamp}2359]"


def parse_feed(xml_text: str) -> tuple[int, list[RawPaper]]:
    """解析 arXiv Atom。**字段全部来自真实响应**，解析失败就让它抛出来（不静默吞）。"""
    root = ET.fromstring(xml_text)
    total_el = root.find(f"{OPENSEARCH}totalResults")
    total = int(total_el.text) if total_el is not None and total_el.text else 0
    items: list[RawPaper] = []
    for entry in root.findall(f"{ATOM}entry"):
        entry_id = (entry.findtext(f"{ATOM}id") or "").strip()
        arxiv_id = entry_id.rsplit("/abs/", 1)[-1] if "/abs/" in entry_id else entry_id
        primary = entry.find(f"{ARXIV_NS}primary_category")
        items.append(
            RawPaper(
                source=SOURCE_ARXIV,
                title=" ".join((entry.findtext(f"{ATOM}title") or "").split()),
                abstract=" ".join((entry.findtext(f"{ATOM}summary") or "").split()),
                authors=[
                    (a.findtext(f"{ATOM}name") or "").strip()
                    for a in entry.findall(f"{ATOM}author")
                ],
                arxiv_id=arxiv_id or None,
                doi=(entry.findtext(f"{ARXIV_NS}doi") or "").strip() or None,
                url=entry_id or None,
                published_at=(entry.findtext(f"{ATOM}published") or "").strip() or None,
                primary_category=primary.get("term") if primary is not None else None,
                categories=[
                    c.get("term", "") for c in entry.findall(f"{ATOM}category") if c.get("term")
                ],
            )
        )
    return total, items


async def fetch_arxiv_ids(
    client: HttpClient,
    ids: list[str],
    endpoint: str,
    batch_size: int = 50,
    *,
    rate_limit_backoff_s: tuple[float, ...] = (20.0, 40.0),
) -> tuple[list[RawPaper], list[str]]:
    """按 arXiv id **批量直采**（`id_list`），返回 `(条目, 未命中的 id)`。

    ★ 经典种子清单专用（v1.11）：ID 确定、无搜索噪声、零配额问题。
    ★ **未命中的 id 必须显式返回** —— arXiv 对不存在的 id 是静默跳过，
      不核对会让"清单 33 条"变成"入库 30 条"却无人察觉（静默失败最危险）。
    ★ **429 必须自己退避重试**（2026-10-26 Actions 实测）：`collect` 刚连发 7 天查询，
      排在其后的 classics 立刻请求 ⇒ `429 Rate exceeded`，整批 49 条全丢。
      `HttpClient` 的通用重试间隔对 arXiv 太短，这里按 20s / 40s 显式退避。
    """
    items: list[RawPaper] = []
    for start in range(0, len(ids), batch_size):
        chunk = ids[start : start + batch_size]
        response = await client.get(
            endpoint,
            params={"id_list": ",".join(chunk), "max_results": batch_size},
            source="arxiv",
            rate_limit="1/3s",
            timeout=None,
        )
        waits = list(rate_limit_backoff_s)
        while response.status_code == 429 and waits:
            await asyncio.sleep(waits.pop(0))
            response = await client.get(
                endpoint,
                params={"id_list": ",".join(chunk), "max_results": batch_size},
                source="arxiv",
                rate_limit="1/3s",
                timeout=None,
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"arXiv HTTP {response.status_code}（id_list={len(chunk)} 项）："
                f"{response.text[:200]}"
            )
        _total, page_items = parse_feed(response.text)
        items.extend(page_items)
    got = {canonical_arxiv_id(raw.arxiv_id) for raw in items if raw.arxiv_id}
    missing = [i for i in ids if canonical_arxiv_id(i) not in got]
    return items, missing


async def fetch_arxiv_day(
    client: HttpClient,
    day: date,
    categories: list[str],
    endpoint: str,
    max_results: int = 600,
) -> list[RawPaper]:
    """抓某一天（按 submittedDate 框定）的论文，自动翻页。"""
    query = submitted_query(categories, day)
    base_params = {
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    items: list[RawPaper] = []
    start = 0
    total: int | None = None
    while len(items) < max_results:
        params = dict(
            base_params, start=start, max_results=min(PAGE_SIZE, max_results - len(items))
        )
        response = await client.get(
            endpoint, params=params, source="arxiv", rate_limit="1/3s", timeout=None
        )
        if response.status_code != 200:
            raise RuntimeError(f"arXiv HTTP {response.status_code}（day={day}）")
        page_total, page_items = parse_feed(response.text)
        total = page_total if total is None else total
        if not page_items:
            break
        items.extend(page_items)
        if len(page_items) < PAGE_SIZE or len(items) >= (total or 0):
            break
        start += PAGE_SIZE
    return items
