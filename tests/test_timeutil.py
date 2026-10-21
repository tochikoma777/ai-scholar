"""时间工具测试。

★ 铁律：代码内禁止 `datetime.now()`，一律 `timeutil.utcnow()`。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_scholar.utils import timeutil


def test_utcnow_is_timezone_aware_utc() -> None:
    now = timeutil.utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_no_datetime_now_in_source(repo_root: Path) -> None:
    """源码里不得出现 `datetime.now()`（渲染层也只能用 `now_local()`）。"""
    offenders: list[str] = []
    for path in (repo_root / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"datetime\.now\((?!tz=UTC\))", text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(repo_root)}:{line}")
    assert not offenders, f"发现 datetime.now()：{offenders}"


def test_ensure_utc_naive_and_aware() -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)
    assert timeutil.ensure_utc(naive).tzinfo == UTC
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timeutil.tz("Asia/Shanghai"))
    assert timeutil.ensure_utc(aware).hour == 4  # 12:00 +08 → 04:00 UTC


def test_parse_duration() -> None:
    assert timeutil.parse_duration("30m") == timedelta(minutes=30)
    assert timeutil.parse_duration("24h") == timedelta(hours=24)
    assert timeutil.parse_duration("7d") == timedelta(days=7)
    assert timeutil.parse_duration(timedelta(hours=1)) == timedelta(hours=1)


def test_window_since_and_last_hours() -> None:
    until = datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)
    start, end = timeutil.window_since("24h", until)
    assert (end - start) == timedelta(hours=24)
    assert end == until
    assert timeutil.last_hours(6, until)[0] == until - timedelta(hours=6)


def test_day_window_uses_local_midnight() -> None:
    """本地日 2026-01-01 的 UTC 窗口起点必须是 2025-12-31 16:00 UTC（+08 的午夜）。"""
    from datetime import date

    start, end = timeutil.day_window(date(2026, 1, 1))
    assert start == datetime(2025, 12, 31, 16, 0, 0, tzinfo=UTC)
    assert (end - start) == timedelta(days=1)


def test_age_days_and_format_local() -> None:
    published = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert timeutil.age_days(published, datetime(2026, 1, 3, 0, 0, 0, tzinfo=UTC)) == 2.0
    assert timeutil.format_local(published, fmt="%Y-%m-%d %H:%M") == "2026-01-01 08:00"
