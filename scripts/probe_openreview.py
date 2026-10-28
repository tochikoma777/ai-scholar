"""探测 OpenReview 真实响应（L9-C：★ 先实测，再决定接不接线）。

回答四个问题（D1 教训：外部源能力**先实测再接线**，ar5iv 的教训是"错误回退会伪装成真数据"）：

1. **可达性**：本机能不能访问 `api2.openreview.net`？（WSL 已知连不上 huggingface.co）
2. **字段**：`notes/search` 返回的 submission 里有没有可用的 venue / venueid？
3. **匹配质量**：按**标题**查库内论文，严格匹配的命中率是多少？
   —— 更要紧的是**误命中率**：搜索 "Direct Preference Optimization" 会返回
   "Distributed Direct Preference Optimization"（另一篇论文）⇒ 必须量化。
4. **成本**：逐篇请求能不能撑住每日新增量（★ 没有按标题的批量接口）。

用法：
    uv run python scripts/probe_openreview.py [--n 20] [--out docs/数据源手册.md] [--no-write]

★ 拿不到真实响应就如实写"不可达 + 原因"，**绝不伪造数字/ fixtures**。
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ai_scholar.utils.http import HttpClient

DEFAULT_DB = "data/ai_scholar.db"
API = "https://api2.openreview.net"
SEARCH_PATH = "/notes/search"
# ★ 严格匹配阈值（低于此视为"另一篇论文"，不算命中）
STRICT_RATIO = 0.90
# 观察区间：用来统计"看着像但不是"的误命中
LOOSE_RATIO = 0.60
MAX_EXCERPT_BYTES = 8 * 1024  # ★ 每源只存 8 KB 代表性片段（项目惯例）


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def sample_papers(db_path: Path, n: int) -> list[tuple[str, str]]:
    """取库内最近 N 篇（id, title）。db 只是**抽样来源**，不是真源。"""
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "select id, title from papers where title is not null order by first_seen_at desc limit ?",
        (n,),
    ).fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows if r[1]]


def pick_best(title: str, notes: list[dict]) -> tuple[float, str | None, str | None, str | None]:
    """从搜索结果里挑最像的一篇，返回 `(相似度, 标题, venue, venueid)`。"""
    best: tuple[float, str | None, str | None, str | None] = (0.0, None, None, None)
    want = _norm(title)
    for note in notes:
        content = note.get("content") or {}
        raw = content.get("title")
        cand = raw.get("value") if isinstance(raw, dict) else raw
        if not isinstance(cand, str):
            continue  # ★ 评论 / review 没有 title，直接跳过（实测这类占多数）
        ratio = difflib.SequenceMatcher(None, want, _norm(cand)).ratio()
        if ratio <= best[0]:
            continue
        venue = content.get("venue")
        venueid = content.get("venueid")
        best = (
            ratio,
            cand,
            venue.get("value") if isinstance(venue, dict) else None,
            venueid.get("value") if isinstance(venueid, dict) else None,
        )
    return best


async def probe(client: HttpClient, papers: list[tuple[str, str]]) -> dict:
    """按标题逐篇查；★ 失败记原因，不重试到假成功。"""
    started = time.monotonic()
    results: list[dict] = []
    errors: list[str] = []
    strict_hits = 0
    loose_only: list[dict] = []  # ★ 疑似误命中（0.60~0.90）
    venues: dict[str, int] = {}

    for paper_id, title in papers:
        try:
            resp = await client.request(
                "GET",
                f"{API}{SEARCH_PATH}",
                source="openreview",
                params={"term": title[:120], "limit": 3},
                # ★ 保守限速（无公开配额文档，先用 1 req/2s 摸底，实测耗时就是成本依据）
                rate_limit="1/2s",
            )
        except httpx.HTTPError as exc:
            errors.append(f"{paper_id}: {type(exc).__name__}")
            continue
        if resp.status_code != 200:
            errors.append(f"{paper_id}: HTTP {resp.status_code}")
            continue
        try:
            payload = resp.json()
        except ValueError:
            errors.append(f"{paper_id}: 响应不是 JSON")
            continue
        notes = payload.get("notes") or []
        ratio, cand, venue, venueid = pick_best(title, notes)
        results.append(
            {
                "id": paper_id,
                "title": title[:80],
                "ratio": round(ratio, 3),
                "candidate": (cand or "")[:80] or None,
                "venue": venue,
                "venueid": venueid,
                "notes": len(notes),
            }
        )
        if ratio >= STRICT_RATIO:
            strict_hits += 1
            if venue:
                venues[venue] = venues.get(venue, 0) + 1
        elif ratio >= LOOSE_RATIO:
            loose_only.append(results[-1])

    elapsed = time.monotonic() - started
    queried = len(results)
    return {
        "probe_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "api": API,
        "queried": queried,
        "errors": errors,
        "elapsed_s": round(elapsed, 2),
        "requests": client.stats.get("requests", 0),
        "strict_hits": strict_hits,
        "strict_hit_rate": round(strict_hits / queried, 3) if queried else None,
        "loose_only": loose_only[:8],
        "venues": dict(sorted(venues.items(), key=lambda kv: -kv[1])),
        "samples": results[:8],
    }


def render(outcome: dict) -> str:
    rate = outcome["strict_hit_rate"]
    rate_txt = f"（{rate:.1%}）" if rate is not None else ""
    lines = [
        "## OpenReview（L9-C 实测）",
        "",
        f"- 探测时间：{outcome['probe_at']} · 接口 `{outcome['api']}{SEARCH_PATH}`",
        f"- 请求 {outcome['requests']} 次（Query {outcome['queried']} 篇）· "
        f"耗时 {outcome['elapsed_s']}s",
        f"- 严格命中（标题相似度 ≥ {STRICT_RATIO}）：**{outcome['strict_hits']} 篇**{rate_txt}",
    ]
    if outcome["errors"]:
        lines += ["", f"- ★ 失败 {len(outcome['errors'])} 条：{'; '.join(outcome['errors'][:5])}"]
    if outcome["venues"]:
        lines += ["", "### 命中的 venue 分布"]
        for venue, count in outcome["venues"].items():
            lines.append(f"- {venue}：{count} 篇")
    if outcome["loose_only"]:
        lines += ["", "### ★ 疑似误命中（相似度 0.60~0.90，不能当命中）"]
        for item in outcome["loose_only"][:5]:
            lines.append(
                f"- `{item['id']}` 库内「{item['title']}」 vs OR「{item['candidate']}」"
                f"（{item['ratio']}）"
            )
    excerpt = json.dumps(outcome["samples"], ensure_ascii=False, indent=2)[:MAX_EXCERPT_BYTES]
    lines += ["", "### 真实响应摘录（字段名以实测为准）", "", "```json", excerpt, "```"]
    return "\n".join(lines) + "\n"


async def _run(n: int) -> dict:
    papers = sample_papers(Path(DEFAULT_DB), n)
    if not papers:
        return {"error": f"库内无论文或 db 不存在（{DEFAULT_DB}）"}
    async with HttpClient() as client:
        return await probe(client, papers)


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenReview 实测（先实测，再决定接不接线）")
    parser.add_argument("--n", type=int, default=20, help="抽样篇数（默认 20）")
    parser.add_argument("--out", default="docs/数据源手册.md", help="写入的手册路径")
    parser.add_argument("--no-write", action="store_true", help="只打印，不写手册")
    args = parser.parse_args()

    outcome = asyncio.run(_run(args.n))
    if "error" in outcome:
        print(f"未获得真实响应：{outcome['error']}")
        return

    print(
        f"请求 {outcome['requests']} 次 / 查询 {outcome['queried']} 篇 / "
        f"耗时 {outcome['elapsed_s']}s"
    )
    print(f"严格命中 {outcome['strict_hits']} 篇（{outcome['strict_hit_rate']}）")
    for venue, count in outcome["venues"].items():
        print(f"  {venue}: {count}")
    if outcome["errors"]:
        print(f"失败 {len(outcome['errors'])} 条（例：{outcome['errors'][0]}）")
    if args.no_write:
        return
    path = Path(args.out)
    section = render(outcome)
    if path.exists() and "## OpenReview" in path.read_text(encoding="utf-8"):
        text = path.read_text(encoding="utf-8")
        head = text.split("## OpenReview", 1)[0]
        path.write_text(head + section, encoding="utf-8")
    else:
        path.write_text(
            (path.read_text(encoding="utf-8") if path.exists() else "") + "\n" + section,
            encoding="utf-8",
        )
    print(f"已写入 {path}")


if __name__ == "__main__":
    main()
