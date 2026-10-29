"""周报 / 月报测试（L9-A）。

★ 判据对齐项目红线：
- 只从 `data/reports/YYYY-MM-DD.json` 真源重算，不读 db / vault；
- **不含生成时间** ⇒ 连续两次产出逐字节一致；
- 坏 JSON / 非法文件名 / 缺字段 ⇒ 显式进 `notes`，绝不补 0 冒充。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ai_scholar.pipeline.recap import (
    RecapReport,
    build_recap,
    load_daily_reports,
    month_window,
    week_window,
    write_recap,
)
from ai_scholar.pipeline.report import DailyReport, ReportHit, RunStatus

REPORTS = Path("data") / "reports"


def _write(tmp_path: Path, day: str, report: DailyReport) -> None:
    (tmp_path / f"{day}.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")


def test_week_window_is_monday_to_sunday() -> None:
    window = week_window(date(2026, 10, 28))  # 周三
    assert window.start == date(2026, 10, 26)
    assert window.end == date(2026, 11, 1)
    assert window.slug == "weekly-2026-W44"
    assert len(window.dates()) == 7


def test_month_window_covers_whole_month() -> None:
    window = month_window(date(2026, 10, 28))
    assert window.start == date(2026, 10, 1)
    assert window.end == date(2026, 10, 31)
    assert window.slug == "monthly-2026-10"


def test_build_recap_aggregates_across_days(tmp_path: Path) -> None:
    """同一篇论文连续两天命中 ⇒ 天数聚合为 2（不是按天重复列两行）。"""
    hit = ReportHit(id="arXiv:1", title="命中两天的论文", score=2.0, groups=["multimodal"])
    _write(
        tmp_path,
        "2026-10-26",
        DailyReport(
            date="2026-10-26",
            new_today=10,
            watchlist_hits=[hit],
            new_graded=[ReportHit(id="arXiv:2", title="定级论文", tier=1)],
            deep_analyzed=[ReportHit(id="arXiv:3", title="分析论文")],
            cost_cny=0.1,
            runs=[RunStatus(run_id="r1", mode="fast")],
            predictions_verified=[
                {"id": "p1", "claim": "c", "status": "hit", "due_at": "2026-11-01"}  # type: ignore[arg-type]
            ],
            degraded={"hf_daily": "timeout"},
        ),
    )
    _write(
        tmp_path,
        "2026-10-27",
        DailyReport(
            date="2026-10-27",
            new_today=5,
            watchlist_hits=[hit],
            new_graded=[ReportHit(id="arXiv:4", title="未定级", tier=None)],
            cost_cny=0.3,
            degraded={"hf_daily": "timeout"},
        ),
    )

    recap = build_recap(tmp_path, window=week_window(date(2026, 10, 27)))
    assert recap.days_covered == 2
    assert recap.days_expected == 7
    assert recap.new_papers == 15
    assert recap.runs_total == 1
    assert recap.top_hits[0].id == "arXiv:1"
    assert recap.top_hits[0].days == 2
    assert recap.graded_total == 2
    assert recap.graded_by_tier == {"Tier 1": 1, "无 Tier 记录": 1}
    assert recap.deep_analyzed_total == 1
    assert recap.cost_total == 0.4
    assert recap.cost_peak_day == "2026-10-27"
    assert recap.cost_avg_cny == 0.2
    assert recap.predictions_hit == 1
    assert recap.hit_rate == 1.0
    # ★ 复发次数：同一个 key 出现 2 天
    assert recap.degraded_days == {"hf_daily": 2}
    assert len(recap.missing_days) == 5


def test_hit_rate_excludes_insufficient_and_none_when_no_sample(tmp_path: Path) -> None:
    """`insufficient` 不进分母（与 `stats` 同口径）；无样本时是 None，不是 0。"""
    _write(tmp_path, "2026-10-26", DailyReport(date="2026-10-26", cost_cny=0.0))
    recap = build_recap(tmp_path, window=week_window(date(2026, 10, 26)))
    assert recap.hit_rate is None
    assert recap.cost_avg_cny == 0.0  # 0 元是真实值（off 档），与"无样本"区分


def test_bad_json_reported_not_silently_dropped(tmp_path: Path) -> None:
    (tmp_path / "2026-10-26.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "weekly-2026-W44.json").write_text("{}", encoding="utf-8")  # 非日报，应跳过
    daily, notes = load_daily_reports(tmp_path)
    assert daily == {}
    assert any("解析失败" in note for note in notes)

    recap = build_recap(tmp_path, window=week_window(date(2026, 10, 26)))
    assert recap.days_covered == 0
    assert any("无日报" in note for note in recap.notes)


def test_missing_reports_dir_is_reported(tmp_path: Path) -> None:
    recap = build_recap(tmp_path / "nope", window=month_window(date(2026, 10, 26)))
    assert recap.days_covered == 0
    assert any("不存在" in note for note in recap.notes)


def test_write_recap_idempotent_and_json_roundtrip(tmp_path: Path) -> None:
    """★ 红线 7：无生成时间 ⇒ 连续两次写出的 md / json 逐字节一致。"""
    _write(tmp_path, "2026-10-26", DailyReport(date="2026-10-26", new_today=3, cost_cny=0.05))
    window = week_window(date(2026, 10, 26))
    first = write_recap(build_recap(tmp_path, window=window), tmp_path)
    second = write_recap(build_recap(tmp_path, window=window), tmp_path)
    assert first.md_path.read_text(encoding="utf-8") == second.md_path.read_text(encoding="utf-8")
    assert first.json_path.read_text(encoding="utf-8") == second.json_path.read_text(
        encoding="utf-8"
    )
    loaded = RecapReport.model_validate(json.loads(first.json_path.read_text(encoding="utf-8")))
    assert loaded == build_recap(tmp_path, window=window)


def test_rendered_markdown_has_all_sections_and_no_timestamp(tmp_path: Path) -> None:
    """判据：板块齐全 + 不出现任何生成时间戳（否则重跑必然有 diff）。"""
    _write(
        tmp_path,
        "2026-10-26",
        DailyReport(
            date="2026-10-26",
            new_today=4,
            watchlist_hits=[ReportHit(id="arXiv:1", title="命中论文", groups=["multimodal"])],
            cost_cny=0.02,
        ),
    )
    window = week_window(date(2026, 10, 26))
    md = write_recap(build_recap(tmp_path, window=window), tmp_path).md_path.read_text(
        encoding="utf-8"
    )
    for section in ("## 采集", "## ★ 命中", "## LLM 产物", "## 成本", "## 推演", "## 降级复发"):
        assert section in md, section
    assert "生成时间" not in md
    assert "- `arXiv:1` — 命中论文（命中 1 天 · multimodal）" in md


def test_previous_period_returns_finished_week_and_month() -> None:
    """★ 自动化口径：周一生成"上周"、1 号生成"上月"（当期还没数据）。"""
    from ai_scholar.pipeline.recap import previous_period

    this_week = week_window(date(2026, 10, 26))  # 周一
    last_week = previous_period(this_week)
    assert (last_week.start, last_week.end) == (date(2026, 10, 19), date(2026, 10, 25))
    assert last_week.slug == "weekly-2026-W43"

    this_month = month_window(date(2026, 10, 1))
    last_month = previous_period(this_month)
    assert (last_month.start, last_month.end) == (date(2026, 9, 1), date(2026, 9, 30))
    assert last_month.slug == "monthly-2026-09"
