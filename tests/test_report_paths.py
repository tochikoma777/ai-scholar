"""报告目录布局测试（L9-D）：`daily/` 优先 + 旧平铺兼容。

★ 背景：`data/reports/*.json` 是**成本累计真源**。路径迁移最危险的失败是
  "读取器漏扫一半文件 ⇒ 成本静默变小"，所以必须把两种布局、同名冲突、非日报文件都钉住。
"""

from __future__ import annotations

from pathlib import Path

from ai_scholar.report_paths import daily_dir, iter_daily_jsons, monthly_dir, weekly_dir


def _touch(path: Path, text: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_reads_legacy_flat_layout(tmp_path: Path) -> None:
    """迁移前：`reports/YYYY-MM-DD.json` 仍要被读到（否则升级即丢历史成本）。"""
    _touch(tmp_path / "reports" / "2026-10-24.json")
    _touch(tmp_path / "reports" / "2026-10-25.json")
    days = [day for day, _ in iter_daily_jsons(tmp_path / "reports")]
    assert days == ["2026-10-24", "2026-10-25"]


def test_reads_daily_subdir_and_prefers_it(tmp_path: Path) -> None:
    """新布局优先：同一天两边都有 ⇒ 用 `daily/` 的那份（不重复计数）。"""
    root = tmp_path / "reports"
    _touch(root / "2026-10-24.json", '{"where":"legacy"}')
    _touch(daily_dir(root) / "2026-10-24.json", '{"where":"daily"}')
    _touch(daily_dir(root) / "2026-10-25.json")
    found = dict(iter_daily_jsons(root))
    assert len(found) == 2
    assert found["2026-10-24"].read_text(encoding="utf-8") == '{"where":"daily"}'


def test_ignores_non_daily_names(tmp_path: Path) -> None:
    """`weekly-*` / `monthly-*` / 随便的 json 都不是日报，不能被当成日报扫进来。"""
    root = tmp_path / "reports"
    _touch(root / "2026-10-24.json")
    _touch(weekly_dir(root) / "weekly-2026-W43.json")
    _touch(monthly_dir(root) / "monthly-2026-09.json")
    _touch(root / "notes.json")
    days = [day for day, _ in iter_daily_jsons(root)]
    assert days == ["2026-10-24"]


def test_missing_root_is_empty(tmp_path: Path) -> None:
    assert iter_daily_jsons(tmp_path / "nope") == []
