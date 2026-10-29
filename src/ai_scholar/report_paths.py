"""报告文件布局（L9-D）：`daily/` `weekly/` `monthly/` 三层 + 旧平铺兼容。

★ 路径只在这里定义：`cost` / `recap` / `site` / `cli` 全部从这里取 ——
  三处各扫一遍目录是"成本统计静默归零"最常见的来源。

★ 兼容策略（2026-10-28 迁移）：先读 `daily/`，再读根目录（迁移前的平铺 `YYYY-MM-DD.json`）；
  同一日期两边都有时**以 `daily/` 为准**。等所有机器都迁移完，根目录那一段可以删。
"""

from __future__ import annotations

import re
from pathlib import Path

DAILY = "daily"
WEEKLY = "weekly"
MONTHLY = "monthly"

# ★ 只认 `YYYY-MM-DD.json`；weekly-/monthly- 不是日报，必须排除
_DATE_JSON = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def daily_dir(reports_root: Path) -> Path:
    return Path(reports_root) / DAILY


def weekly_dir(reports_root: Path) -> Path:
    return Path(reports_root) / WEEKLY


def monthly_dir(reports_root: Path) -> Path:
    return Path(reports_root) / MONTHLY


def iter_daily_jsons(reports_root: Path) -> list[tuple[str, Path]]:
    """所有日报 `(YYYY-MM-DD, path)`，按日期升序。

    ★ 新目录优先：同一日期若 `daily/` 与根目录都有文件，只返回 `daily/` 的那份。
    """
    root = Path(reports_root)
    found: dict[str, Path] = {}
    for folder in (root, daily_dir(root)):  # 先旧后新 ⇒ 新覆盖旧
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            match = _DATE_JSON.match(path.name)
            if match:
                found[match.group(1)] = path
    return sorted(found.items(), key=lambda item: item[0])
