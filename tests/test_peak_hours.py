"""DeepSeek 高峰时段守卫（2026-11-11）。

★ 官方定义（2026-08-17 生效）：高峰 = 北京时间周一至周五 9:00–12:00 / 14:00–18:00
  = UTC 01:00–04:00 / 06:00–10:00；其余（含周末、法定节假日）为空闲半价。
★ 这里只测**窗口边界与降级行为**；「当前是否已在空闲」是运行事实，写进 `docs/成本预算.md` §13。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ai_scholar.utils.peak_hours import MARGIN_MIN, PEAK_WINDOWS_UTC, is_peak, main

MONDAY = (2026, 11, 9)  # 2026-11-09 是周一
SATURDAY = (2026, 11, 7)


def _utc(day: tuple[int, int, int], hour: int, minute: int) -> datetime:
    return datetime(*day, hour, minute, tzinfo=UTC)


def test_peak_window_constants_match_official_definition() -> None:
    """窗口常量必须与官方一致（UTC 01:00–04:00 / 06:00–10:00，前后各 15 分钟余量）。"""
    assert PEAK_WINDOWS_UTC == ((60, 240), (360, 600))
    assert MARGIN_MIN == 15


def test_peak_boundaries_weekday() -> None:
    """工作日窗口边界：余量内算高峰，余量外算空闲。"""
    assert not is_peak(_utc(MONDAY, 0, 44))  # 00:44 < 00:45
    assert is_peak(_utc(MONDAY, 0, 45))  # 进入余量
    assert is_peak(_utc(MONDAY, 1, 0))  # 高峰正窗
    assert is_peak(_utc(MONDAY, 4, 14))  # 04:14 < 04:15
    assert not is_peak(_utc(MONDAY, 4, 15))  # 退出余量
    assert not is_peak(_utc(MONDAY, 5, 44))  # 第二段余量从 05:45 起
    assert is_peak(_utc(MONDAY, 5, 45))
    assert is_peak(_utc(MONDAY, 6, 0))  # 第二段正窗起点
    assert is_peak(_utc(MONDAY, 10, 14))
    assert not is_peak(_utc(MONDAY, 10, 15))


def test_weekend_is_all_off_peak() -> None:
    assert not is_peak(_utc(SATURDAY, 9, 0))
    assert not is_peak(_utc(SATURDAY, 10, 0))
    assert not is_peak(_utc((2026, 11, 8), 14, 0))  # 周日


def test_main_degrades_to_off_in_peak(tmp_path: Path, monkeypatch) -> None:
    """高峰 ⇒ 往 `$GITHUB_ENV` 写 `AI_SCHOLAR_MODE=off`（off 档让 LLM 三步零调用）。"""
    env_file = tmp_path / "github_env"
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    assert main(now=_utc(MONDAY, 2, 0)) == 0
    assert env_file.read_text(encoding="utf-8") == "AI_SCHOLAR_MODE=off\n"


def test_main_keeps_mode_when_off_peak(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "github_env"
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    assert main(now=_utc(MONDAY, 16, 30)) == 0  # 北京次日 00:30，空闲
    assert not env_file.exists() or env_file.read_text(encoding="utf-8") == ""
