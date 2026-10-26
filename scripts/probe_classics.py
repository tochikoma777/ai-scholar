"""经典种子清单核对：对 `config/classics.yaml` 的 id 发**真实请求**，逐条比对标题。

★ 为什么要有这个脚本（D1 / E5）：arXiv 的 `id_list` 对**不存在的 id 静默跳过**，
id 写错还会拉到别的论文 —— 不实测就声称"清单已核验"，是典型的"推断替代实测"。

用法：
    uv run python scripts/probe_classics.py           # 打印 id → 实际标题 + 未命中
    uv run python scripts/probe_classics.py --fix     # 把实际标题写回 note（人工复核后再提交）

★ 退出码：0 = 全部命中；1 = 有未命中的 id（清单必须修）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_scholar.collectors.arxiv import fetch_arxiv_ids  # noqa: E402
from ai_scholar.config import load_classics, load_settings  # noqa: E402
from ai_scholar.utils.http import HttpClient  # noqa: E402


def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


async def main(fix: bool) -> int:
    classics = load_classics()
    ids = classics.ids()
    if not ids:
        print("config/classics.yaml 为空或不存在 —— 无需核对")
        return 0

    settings = load_settings()
    cfg = settings.source("arxiv")
    endpoint = cfg.endpoint if cfg else "https://export.arxiv.org/api/query"

    async with HttpClient(user_agent=settings.http.user_agent) as client:
        items, missing = await fetch_arxiv_ids(client, ids, endpoint)

    by_id: dict[str, str] = {}
    for raw in items:
        if raw.arxiv_id:
            by_id[raw.arxiv_id.split("v")[-1] if "v" in raw.arxiv_id else raw.arxiv_id] = raw.title
    # ★ 兼容带版本号的返回（如 1706.03762v7）
    for raw in items:
        if raw.arxiv_id:
            base = raw.arxiv_id.split("v")[0]
            by_id.setdefault(base, raw.title)

    print(f"清单 {len(ids)} 条 · 命中 {len(items)} 条 · 未命中 {len(missing)} 条\n")
    mismatch: list[str] = []
    for group in classics.groups:
        print(f"[{group.name}]")
        for paper in group.papers:
            actual = by_id.get(paper.id, "—— 未命中 ——")
            if paper.title:
                ok = "OK  " if _norm(paper.title) == _norm(actual) else "DIFF"
            else:
                # 没写实测标题时退化为包含匹配（宽松，仅提示）
                ok = "OK  " if _norm(paper.note) in _norm(actual) else "DIFF"
            if ok == "DIFF":
                mismatch.append(paper.id)
            print(f"  {ok} {paper.id}  简={paper.note!r}  实际={actual!r}")
        print()

    if missing:
        print("未命中的 id（清单必须修）：" + ", ".join(missing))
    if mismatch:
        print("标题与 note 不符（id 可能指向了别的论文）：" + ", ".join(mismatch))

    if fix and not missing:
        path = Path(__file__).resolve().parents[1] / "config" / "classics.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for g in data.get("groups", []):
            for p in g.get("papers", []):
                actual = by_id.get(p["id"])
                if actual:
                    p["note"] = " ".join(actual.split())
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8",
        )
        print(f"\n已把实际标题写回 {path}（★ 提交前请人工复核格式）")

    return 1 if (missing or mismatch) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="把实际标题写回 note")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.fix)))
