"""报告文件布局：`daily|weekly|monthly` + 年月归档 + 旧平铺兼容。

当前写入（2026-11-12 起）：
- 日报 `data/reports/daily/YYYY/MM/YYYY-MM-DD.{md,json}`
- 周报 `data/reports/weekly/YYYY/weekly-YYYY-Www.{md,json}`
- 月报 `data/reports/monthly/YYYY/monthly-YYYY-MM.{md,json}`

★ 路径只在这里定义：`cost` / `recap` / `site` / `cli` 全部从这里取 ——
  三处各扫一遍目录是"成本统计静默归零"最常见的来源。

★ 兼容策略：读取一律 **rglob + 文件名正则**（同时认旧平铺与 `daily/` 中层）；
  同一日期多处存在时路径更深者优先。写入永远走上面的归档结构。
"""

from __future__ import annotations

import re
from pathlib import Path

DAILY = "daily"
WEEKLY = "weekly"
MONTHLY = "monthly"

# ★ 只认 `YYYY-MM-DD.json`；weekly-/monthly- 不是日报，必须排除
_DATE_JSON = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
_YEAR = re.compile(r"(\d{4})")


def daily_dir(reports_root: Path) -> Path:
    return Path(reports_root) / DAILY


def weekly_dir(reports_root: Path) -> Path:
    return Path(reports_root) / WEEKLY


def monthly_dir(reports_root: Path) -> Path:
    return Path(reports_root) / MONTHLY


def archived_subdir(base: Path, day: str) -> Path:
    """`YYYY-MM-DD` → `base/YYYY/MM`（写入用；读取一律走 rglob）。"""
    text = str(day)[:10]
    return Path(base) / text[:4] / text[5:7]


def recap_subdir(base: Path, slug: str) -> Path:
    """`weekly-2026-W45` / `monthly-2026-10` → `base/2026`。"""
    match = _YEAR.search(slug)
    return Path(base) / (match.group(1) if match else "")


def iter_daily_jsons(reports_root: Path) -> list[tuple[str, Path]]:
    """所有日报 `(YYYY-MM-DD, path)`，按日期升序（★ 递归，兼容旧平铺）。

    ★ 同一日期多处存在时，**路径更深（新归档结构）优先**；再按路径字典序取最后一个，
      保证 `data/reports/daily/2026/11/x.json` 覆盖 `data/reports/daily/x.json`。
    """
    root = Path(reports_root)
    found: dict[str, Path] = {}
    if not root.is_dir():
        return []
    for path in sorted(root.rglob("*.json")):
        match = _DATE_JSON.match(path.name)
        if not match:
            continue
        day = match.group(1)
        previous = found.get(day)
        if previous is None or (len(path.parts), str(path)) > (len(previous.parts), str(previous)):
            found[day] = path
    return sorted(found.items(), key=lambda item: item[0])
