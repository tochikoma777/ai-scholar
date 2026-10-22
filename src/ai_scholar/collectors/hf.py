"""HF Daily Papers 采集器。

约束（任务卡 §2.4）：
- ★ 走 `hf_base_url` 配置（默认官方，可填镜像）
- **不可达 → warning + 跳过，不计失败、不影响退出码**（由 pipeline 捕获处理）
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from ai_scholar.collectors.base import SOURCE_HF, RawPaper
from ai_scholar.utils.http import HttpClient


def parse_daily_papers(payload: Any) -> list[RawPaper]:
    """解析 HF daily_papers 响应。**字段来自真实响应**（不凭印象补）。"""
    if not isinstance(payload, list):
        raise ValueError(f"HF daily_papers 响应不是数组：{type(payload).__name__}")
    items: list[RawPaper] = []
    for row in payload or []:
        paper = (row or {}).get("paper") or {}
        authors = [
            str(a.get("name") or "")
            for a in (paper.get("authors") or [])
            if isinstance(a, dict) and a.get("name")
        ]
        items.append(
            RawPaper(
                source=SOURCE_HF,
                title=" ".join(str(paper.get("title") or "").split()),
                abstract=" ".join(str(paper.get("summary") or "").split()),
                authors=authors,
                # ★ HF 的 paper.id 就是 arXiv 短 id（跨源合并的连接键）
                arxiv_id=str(paper.get("id") or "") or None,
                url=f"https://huggingface.co/papers/{paper.get('id')}" if paper.get("id") else None,
                published_at=str(paper.get("publishedAt") or "") or None,
                # ★ upvotes 只是热度信号，**不是身份依据**
                hf_score=int(paper["upvotes"]) if paper.get("upvotes") is not None else None,
            )
        )
    return items


async def fetch_hf_day(client: HttpClient, day: date, base_url: str) -> list[RawPaper]:
    """抓某一天的上榜论文。不可达时抛异常（由 pipeline 转成 warning + 跳过）。"""
    params = {"date": day.isoformat()}
    try:
        response = await client.get(
            base_url, params=params, source="hf_daily", rate_limit="10/1s", timeout=None
        )
    except (httpx.HTTPError, OSError) as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(f"HF HTTP {response.status_code}")
    return parse_daily_papers(response.json())
