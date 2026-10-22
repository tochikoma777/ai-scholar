"""arXiv 采集器。

约束（任务卡 §2.4）：
- ★ **https**（http 会 301）
- 限速约 1 req / 3s（走 `HttpClient` 的令牌桶）
- 单页最多 200，自动翻页
- 按 `submittedDate` 精确框定某一天（不是"取最近 N 条"）
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from ai_scholar.collectors.base import SOURCE_ARXIV, RawPaper
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
