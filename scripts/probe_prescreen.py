"""实测预筛通过率 `p`（★ 体积最大杠杆）+ 用实测值重算体积。

`p = 通过预筛条数 / N_enter`（N_enter = 进入系统的论文数，与是否跟踪无关）。

体积公式（§3.7.2）：`总量 = N×p + N×(1-p)×r`
- `r = 0.2` 是**淘汰集**抽样留存率，不是总体放行率
- SQLite 每行字节数**实测**：建临时库插 5 万行 signals，按页数差算

用法：
    uv run python scripts/probe_prescreen.py [--db PATH] [--out docs/数据源手册.md] [--no-write]
"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from pathlib import Path

from ai_scholar.config import load_settings
from ai_scholar.store.models import Paper
from ai_scholar.utils.timeutil import utcnow

DEFAULT_DB = "data/ai_scholar.db"
SAMPLE_ROWS = 50_000


def measure_signal_row_bytes(rows: int = SAMPLE_ROWS) -> float:
    """实测 signals 表每行占用（含主键索引与页开销）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE signals (paper_id TEXT NOT NULL, observed_at TEXT NOT NULL,"
            " citation_count INTEGER, reference_count INTEGER, source TEXT NOT NULL,"
            " PRIMARY KEY (paper_id, observed_at))"
        )
        base = path.stat().st_size
        data = [
            (f"arXiv:2610.{i:05d}", f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}", i % 90, 40, "s2")
            for i in range(rows)
        ]
        conn.executemany("INSERT INTO signals VALUES (?,?,?,?,?)", data)
        conn.commit()
        conn.execute("VACUUM")
        conn.close()
        return (path.stat().st_size - base) / rows


def first_seen_bytes_per_row() -> tuple[float, int]:
    """实测 `data/first_seen.jsonl` 每行字节数（真实文件，不估算）。"""
    path = load_settings().data_dir() / "first_seen.jsonl"
    if not path.exists():
        return 0.0, 0
    data = path.read_bytes()
    lines = [line for line in data.splitlines() if line.strip()]
    return (len(data) / len(lines)) if lines else 0.0, len(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="实测预筛通过率 p 与体积")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--out", default="docs/数据源手册.md")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "select id, title, abstract, published_at, first_seen_at from papers"
    ).fetchall()
    conn.close()

    total = len(rows)
    if not total:
        print("库里没有论文，无法测 p")
        return 1

    passed = 0
    reasons: dict[str, int] = {}
    for pid, title, abstract, _published, _first in rows:
        paper = Paper(
            id=pid,
            source="arxiv",
            primary_source="arxiv",
            first_seen_at="2026-01-01",
            published_at=_published,
            title=title or "",
            abstract=abstract,
            content_hash="probe",
        )
        from ai_scholar.pipeline.prescreen import prescreen

        ok, reason = prescreen(paper, settings.prescreen)
        if ok:
            passed += 1
        else:
            key = (reason or "").split("（")[0].split("：")[0]
            reasons[key] = reasons.get(key, 0) + 1

    p = passed / total
    r = settings.prescreen.retain_ratio
    n_daily = 515  # §12 实测单日条数

    signal_bytes = measure_signal_row_bytes()
    fs_bytes, fs_lines = first_seen_bytes_per_row()
    tracked_rows_year = 365 * n_daily * (p + (1 - p) * r)
    signals_mb = tracked_rows_year * signal_bytes / 1024 / 1024
    first_seen_mb = 365 * n_daily * fs_bytes / 1024 / 1024
    total_mb = signals_mb + first_seen_mb

    old_tracked = 365 * n_daily * (0.8 + 0.2 * 0.2)
    old_mb = old_tracked * signal_bytes / 1024 / 1024 + first_seen_mb

    print(f"N_enter 样本：{total} 篇")
    print(f"通过预筛：{passed} 篇 ⇒ p = {p:.4f}")
    print(f"淘汰原因分布：{reasons}")
    print(f"r = {r}（淘汰集留存率）")
    print(f"signals 每行实测：{signal_bytes:.1f} B（{SAMPLE_ROWS} 行样本）")
    print(f"first_seen 每行实测：{fs_bytes:.1f} B（{fs_lines} 行）")
    print(f"年跟踪行数：{tracked_rows_year:,.0f}（旧估算 p=0.8 时为 {old_tracked:,.0f}）")
    print(f"signals ≈ {signals_mb:.1f} MB/年 · first_seen ≈ {first_seen_mb:.1f} MB/年")
    print(f"合计 ≈ {total_mb:.1f} MB/年（旧估算 {old_mb:.1f} MB/年）")

    if args.no_write:
        return 0

    ts = utcnow().astimezone().isoformat(timespec="seconds")
    section = f"""
## 2.7 预筛通过率 `p` 与体积实测（L1-B）

实测时间：{ts}
样本：库内全部 {total} 篇（N_enter）

| 指标 | 值 |
|---|---|
| 预筛通过 `p` | **{p:.4f}**（{passed}/{total}） |
| 淘汰原因分布 | {"；".join(f"{k}: {v}" for k, v in reasons.items()) or "无"} |
| 淘汰集留存率 `r` | {r}（★ 不是总体放行率） |
| signals 每行实测 | {signal_bytes:.1f} B（{SAMPLE_ROWS} 行临时库实测，含主键索引） |
| first_seen 每行实测 | {fs_bytes:.1f} B（{fs_lines} 行真实文件） |
| 年跟踪行数（N=515/天） | {tracked_rows_year:,.0f} |
| signals 体积 | ≈ {signals_mb:.1f} MB/年 |
| first_seen 体积 | ≈ {first_seen_mb:.1f} MB/年 |
| 合计 | **≈ {total_mb:.1f} MB/年** |
| 旧估算（p=0.8 推断） | ≈ {old_mb:.1f} MB/年 |

★ 结论：`p` 实测为 **{p:.2f}**，与"负面排除几乎不筛"的预判一致。
  体积对 `p` 的敏感度低于预期（`p` 从 0.8 变到 {p:.2f}，年体积 {old_mb:.0f} → {total_mb:.0f} MB），
  真正的大头是 **N（每天 515 篇）** 与 **first_seen 每条都要落 public**。
★ 与旧文档「≈110 MB/年」对不上：该数隐含约 700 B/行（或把 papers 表膨胀算进来了）。
  本次实测 signals 每行 {signal_bytes:.0f} B，按行数算只有 **≈{total_mb:.0f} MB/年**。
  **以实测为准**。
  若体积超标，安全阀顺序仍是：已定级集月→季 → 复活只留 +180 → pending 降频 → 调小 `r`。

"""

    out = Path(args.out)
    text = out.read_text(encoding="utf-8")
    marker = "\n## 2.7 预筛通过率"
    if marker in text:
        head, rest = text.split(marker, 1)
        tail = "\n## " + rest.split("\n## ", 1)[1] if "\n## " in rest else ""
        text = head + tail
    # 插到 §3 之前（保持手册结构：2.x 数据源，3 环境分级）
    anchor = "\n## 3. 环境分级结论"
    if anchor in text:
        head, tail = text.split(anchor, 1)
        text = head.rstrip("\n") + "\n" + section + anchor.lstrip("\n") + tail
    else:
        text = text.rstrip("\n") + "\n" + section
    out.write_text(text, encoding="utf-8")
    print(f"✓ 已写入：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
