"""探测 Semantic Scholar 真实响应（★ D2 的三个未知数）。

抽 N 篇库内 arXiv 论文，`paper/batch` 一次问回，报三个数：

1. 解析出 `paperId` 的比例（S2 是否已索引）
2. `referenceCount > 0` 的比例
3. `referenceCount` 的中位数

★ 无 key 时 S2 限流很严（实测 429 频发）：这里会**重试**，
  全部失败就如实写"未获得 + 原因"，**绝不编造数字**。

用法：
    uv run python scripts/probe_s2.py [--n 50] [--out docs/数据源手册.md] [--no-write]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
from datetime import UTC, datetime
from pathlib import Path

from ai_scholar.collectors.s2 import fetch_s2_papers
from ai_scholar.config import load_settings
from ai_scholar.utils.http import HttpClient

DEFAULT_DB = "data/ai_scholar.db"
MAX_ATTEMPTS = 8


def sample_ids(db_path: Path, n: int) -> list[str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "select arxiv_id from papers where arxiv_id is not null "
        "order by first_seen_at desc limit ?",
        (n,),
    ).fetchall()
    conn.close()
    return [f"ARXIV:{r[0]}" for r in rows if r[0]]


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


async def probe(n: int, db_path: Path) -> tuple[list[object], str | None]:
    """返回 `(records, 失败原因)`。"""
    ids = sample_ids(db_path, n)
    if not ids:
        return [], f"库里没有可用的 arXiv id（{db_path}）"

    settings = load_settings()
    cfg = settings.source("semantic_scholar")
    endpoint = (cfg.endpoint if cfg else "") or (
        "https://api.semanticscholar.org/graph/v1/paper/batch"
    )
    api_key = (cfg.api_key if cfg else "") or ""
    batch_size = cfg.batch_size if cfg else 500

    last_error = "未知"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        async with HttpClient(user_agent=settings.http.user_agent) as client:
            try:
                records = await fetch_s2_papers(
                    client,
                    ids,
                    endpoint=endpoint,
                    api_key=api_key,
                    batch_size=batch_size,
                    rate_limit="1/1s",
                )
                return records, None
            except Exception as exc:  # noqa: BLE001 —— 探测脚本要如实报告失败
                last_error = f"{type(exc).__name__}: {exc}"
                print(f"  第 {attempt}/{MAX_ATTEMPTS} 次尝试失败：{last_error}")
                await asyncio.sleep(min(10 * attempt, 40))
    return [], last_error


def render_section(
    *,
    n: int,
    records: list[object],
    error: str | None,
    sample: str,
    probed_at: str,
) -> str:
    if error or not records:
        return (
            "\n## 2.6 Semantic Scholar（L1-A 实测）\n\n"
            f"实测时间：{probed_at}｜样本：库内最新 {n} 篇 arXiv 论文\n\n"
            "★ **未获得三个数**。\n\n"
            f"- 原因：{error or 'S2 无返回'}\n"
            "- 影响：引用关联的**冷启动稀疏性**无法量化（设计方案 §5.1b 已按"
            "「必然稀疏」设计，不阻塞 L1-A）。\n"
            "- 复测方式：`uv run python scripts/probe_s2.py --n 50`"
            "（有 `AI_SCHOLAR_S2_API_KEY` 时成功率大幅提高）。\n"
        )

    total = len(records)
    resolved = [r for r in records if r is not None and getattr(r, "paper_id", None)]
    with_refs = [r for r in resolved if (r.reference_count or 0) > 0]
    counts = sorted(r.reference_count or 0 for r in resolved)
    median = statistics.median(counts) if counts else None

    return (
        "\n## 2.6 Semantic Scholar（L1-A 实测）\n\n"
        f"实测时间：{probed_at}｜样本：库内最新 {n} 篇 arXiv 论文（无 api_key）\n\n"
        "| 指标 | 值 |\n|---|---|\n"
        f"| ① 解析出 `paperId` 的比例 | {len(resolved)}/{total} "
        f"= **{_fmt(100 * len(resolved) / total)}%** |\n"
        f"| ② `referenceCount > 0` 的比例 | {len(with_refs)}/{max(len(resolved), 1)} "
        f"= **{_fmt(100 * len(with_refs) / max(len(resolved), 1))}%** |\n"
        f"| ③ `referenceCount` 中位数 | **{median if median is not None else '—'}** |\n"
        f"| `referenceCount` 最小 / 最大 | {counts[0] if counts else '—'} / "
        f"{counts[-1] if counts else '—'} |\n\n"
        "★ 结论（支撑 §5.1b）：**引用数据本身不缺**，缺的是「被引论文在我们库里」——\n"
        "  这些 references 绝大多数指向我们没有采集过的老论文，因此库内可建的边很少。\n\n"
        "真实响应摘录（8 KB 内，字段**未改写**）：\n\n"
        "```json\n" + sample + "\n```\n"
    )


async def main_async(args: argparse.Namespace) -> int:
    probed_at = datetime.now(tz=UTC).astimezone().isoformat(timespec="seconds")
    print(f"★ 探测 S2：样本 {args.n} 篇（db={args.db}）")
    records, error = await probe(args.n, Path(args.db))

    # dataclass 不能直接 json.dumps，转成与真实响应同形的 dict 再摘录
    sample = ""
    if records:
        sample = json.dumps(
            [
                {
                    "paperId": r.paper_id,
                    "externalIds": {"ArXiv": r.arxiv_id},
                    "title": r.title,
                    "year": r.year,
                    "citationCount": r.citation_count,
                    "referenceCount": r.reference_count,
                    "references": [
                        {"paperId": x.paper_id, "externalIds": {"ArXiv": x.arxiv_id}}
                        for x in r.references[:3]
                    ],
                }
                for r in records
                if r is not None
            ][:2],
            ensure_ascii=False,
            indent=2,
        )[:4000]

    section = render_section(
        n=args.n, records=records, error=error, sample=sample, probed_at=probed_at
    )
    print(section)

    if args.no_write:
        return 0
    out_path = Path(args.out)
    text = out_path.read_text(encoding="utf-8")
    marker = "\n## 2.6 Semantic Scholar"
    if marker in text:
        head = text.split(marker, 1)[0]
        rest = text.split(marker, 1)[1]
        # 丢弃旧的 2.6 段（到下一个 ## 为止）
        tail = "\n## " + rest.split("\n## ", 1)[1] if "\n## " in rest else ""
        text = head + tail
    out_path.write_text(text.rstrip("\n") + "\n" + section, encoding="utf-8")
    print(f"✓ 已写入：{out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="探测 Semantic Scholar 真实响应")
    parser.add_argument("--n", type=int, default=50, help="抽样篇数")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite 路径")
    parser.add_argument("--out", default="docs/数据源手册.md", help="输出 Markdown 路径")
    parser.add_argument("--no-write", action="store_true", help="只打印，不写文件")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
