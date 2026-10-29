"""L8-F：`cost` 命令与成本汇总（真源 = data/reports/*.json）。

★ 三条不让步的约束：
1. **累计以 reports 为真源**（public、可审计）；db 只用于分项，且要标注"缓存"。
2. 缺样本时显示"不可用"，**不拿 0 冒充**中位数/环比。
3. 坏报告文件必须被报出来（静默跳过 = 让成本账悄悄少算）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel

from ai_scholar.budget import BudgetSettings
from ai_scholar.config import Settings
from ai_scholar.pipeline.cost import (
    build_summary,
    detect_anomalies,
    load_report_costs,
    load_usage_by_purpose,
    summarize_reports,
)

TODAY = date(2026, 10, 26)


def _settings(tmp_path: Path) -> Settings:
    settings = Settings()
    settings.app.data_dir = str(tmp_path / "data")
    return settings


def _write_report(folder: Path, day: str, cost: float | None, *, raw: str | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    payload = raw if raw is not None else json.dumps({"date": day, "cost_cny": cost})
    (folder / f"{day}.json").write_text(payload, encoding="utf-8")


# ── ① 读真源 ────────────────────────────────────────────────────────────
def test_load_reports_ok(tmp_path: Path) -> None:
    _write_report(tmp_path / "reports", "2026-10-24", 0.1)
    _write_report(tmp_path / "reports", "2026-10-25", 0.2)
    costs, bad = load_report_costs(tmp_path)
    assert costs == {date(2026, 10, 24): 0.1, date(2026, 10, 25): 0.2}
    assert bad == []


def test_load_reports_reports_bad_files(tmp_path: Path) -> None:
    """★ 坏 JSON / cost 为 null / date 非法 都要显式报出来。"""
    _write_report(tmp_path / "reports", "2026-10-24", 0.1)
    _write_report(tmp_path / "reports", "2026-10-25", None)
    _write_report(tmp_path / "reports", "2026-10-26", 0.0, raw="{not json")
    # ★ 文件名仍是日期形状、但 payload 里的 date 非法（这是真实故障形态：
    #   文件名不是日报的直接被忽略，属"不是日报"而不是"坏日报"）
    (tmp_path / "reports" / "2026-10-27.json").write_text(
        '{"date": "bad", "cost_cny": 1}', encoding="utf-8"
    )
    costs, bad = load_report_costs(tmp_path)
    assert costs == {date(2026, 10, 24): 0.1}
    assert len(bad) == 3, bad


def test_load_reports_missing_dir(tmp_path: Path) -> None:
    costs, bad = load_report_costs(tmp_path)
    assert costs == {} and bad == []


# ── ② 窗口汇总 ──────────────────────────────────────────────────────────
def test_summarize_windows() -> None:
    costs = {
        date(2026, 9, 30): 1.0,  # 上月
        date(2026, 10, 1): 0.5,  # 本月
        date(2026, 10, 20): 0.2,
        date(2026, 10, 25): 0.2,
        date(2026, 10, 26): 0.4,
        date(2025, 12, 31): 9.0,  # 去年
    }
    summary = summarize_reports(costs, today=TODAY)
    assert summary.today == 0.4
    assert summary.week == 0.8  # 10-20..10-26
    assert summary.month == 1.3  # 10-01..10-26
    assert summary.year == 2.3  # 本年（含 9-30? 不，9-30 也是本年）
    assert summary.total == 11.3


def test_summarize_year_includes_earlier_months() -> None:
    costs = {date(2026, 1, 5): 0.3, date(2026, 10, 26): 0.1, date(2025, 6, 1): 5.0}
    summary = summarize_reports(costs, today=TODAY)
    assert summary.year == 0.4
    assert summary.total == 5.4


def test_median_excludes_today_and_handles_empty() -> None:
    costs = {date(2026, 10, 24): 0.1, date(2026, 10, 25): 0.3, date(2026, 10, 26): 9.9}
    summary = summarize_reports(costs, today=TODAY)
    assert summary.median_7d == 0.2  # (0.1+0.3)/2，今日不参与
    assert summary.today_delta_pct == round((9.9 - 0.2) / 0.2 * 100, 1)

    empty = summarize_reports({}, today=TODAY)
    assert empty.median_7d is None, "没有样本必须是 None，不能是 0"
    assert empty.today_delta_pct is None
    assert any("没有可用的历史报告" in note for note in empty.notes)


def test_window_days_configurable() -> None:
    costs = {date(2026, 10, 20): 0.1, date(2026, 10, 26): 0.1}
    assert summarize_reports(costs, today=TODAY, window_days=7).week == 0.2
    assert summarize_reports(costs, today=TODAY, window_days=3).week == 0.1


# ── ③ 异常判定（三条规则都要能单独触发）────────────────────────────────
def _summary(**kwargs: object):
    from ai_scholar.pipeline.cost import CostSummary

    return CostSummary(**kwargs)  # type: ignore[arg-type]


def test_anomaly_daily_half_limit() -> None:
    budget = BudgetSettings(daily_limit_cny=1.0, monthly_limit_cny=40.0)
    reasons = detect_anomalies(_summary(today=0.6), budget=budget, today=TODAY)
    assert any("日限额一半" in r for r in reasons)


def test_anomaly_relative_spike() -> None:
    budget = BudgetSettings(daily_limit_cny=10.0, monthly_limit_cny=400.0)
    reasons = detect_anomalies(_summary(today=0.9, median_7d=0.2), budget=budget, today=TODAY)
    assert any("中位数" in r for r in reasons)


def test_anomaly_month_ratio() -> None:
    budget = BudgetSettings(daily_limit_cny=10.0, monthly_limit_cny=1.0)
    reasons = detect_anomalies(_summary(today=0.0, month=0.9), budget=budget, today=TODAY)
    assert any("月限额 80%" in r for r in reasons)


def test_no_anomaly_when_normal() -> None:
    budget = BudgetSettings(daily_limit_cny=2.0, monthly_limit_cny=40.0)
    assert detect_anomalies(_summary(today=0.1, month=1.0), budget=budget, today=TODAY) == []


# ── ④ 分项（db，缓存口径）────────────────────────────────────────────────
def test_usage_by_purpose(tmp_path: Path) -> None:
    from ai_scholar.store.models import LLMUsage

    settings = _settings(tmp_path)
    db = settings.db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    from ai_scholar.store.db import engine_for

    engine = engine_for(db)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            LLMUsage(
                run_id="r1",
                model="m",
                tokens_in=1,
                tokens_out=1,
                cost_cny=0.02,
                purpose="grading",
                created_at="2026-10-26T01:00:00+00:00",
            )
        )
        session.add(
            LLMUsage(
                run_id="r2",
                model="m",
                tokens_in=1,
                tokens_out=1,
                cost_cny=0.05,
                purpose="deep_analysis",
                created_at="2026-10-26T02:00:00+00:00",
            )
        )
        session.commit()

    by_purpose, rows, note = load_usage_by_purpose(db, since=TODAY.replace(day=1))
    assert by_purpose == {"grading": 0.02, "deep_analysis": 0.05}
    assert rows == 2 and note is None


def test_usage_missing_db_is_a_note_not_a_crash(tmp_path: Path) -> None:
    by_purpose, rows, note = load_usage_by_purpose(tmp_path / "nope.db", since=TODAY)
    assert by_purpose == {} and rows == 0
    assert note and "db 不存在" in note


# ── ⑤ 端到端：build_summary + CLI ───────────────────────────────────────
def test_build_summary_end_to_end(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_report(settings.data_dir() / "reports", "2026-10-25", 0.1)
    _write_report(settings.data_dir() / "reports", "2026-10-26", 0.2)
    summary = build_summary(settings=settings, today=TODAY)
    assert summary.today == pytest.approx(0.2)
    assert summary.total == pytest.approx(0.3)
    assert summary.report_days == 2
    assert summary.has_anomaly is False


def test_cli_cost_json(tmp_path: Path, monkeypatch) -> None:
    import json as _json

    from typer.testing import CliRunner

    from ai_scholar.cli import app

    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    _write_report(tmp_path / "data" / "reports", "2026-10-25", 0.1)
    result = CliRunner().invoke(app, ["cost", "--json"])
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    assert set(payload) >= {"today", "week", "month", "year", "total", "anomaly", "by_purpose"}
    assert payload["total"] == 0.1
    assert "缓存" in payload["by_purpose_source"]


# ── ⑥ 报告实时 guard（今日成本来自 db，不重复计今天的旧报告）─────────────
def test_live_anomalies_uses_today_cost_without_double_count(tmp_path: Path) -> None:
    from ai_scholar.pipeline.cost import live_anomalies

    reports = tmp_path / "reports"
    _write_report(reports, "2026-10-24", 0.10)
    _write_report(reports, "2026-10-25", 0.10)
    # ★ 今天已有一份旧报告（重跑场景）：不能被重复计入 month
    _write_report(reports, "2026-10-26", 5.00)

    budget = BudgetSettings(daily_limit_cny=1.0, monthly_limit_cny=1000.0)
    reasons = live_anomalies(data_dir=tmp_path, today=TODAY, today_cost=0.9, budget=budget)
    assert any("日限额一半" in r for r in reasons)
    assert any("中位数" in r for r in reasons)

    normal = live_anomalies(data_dir=tmp_path, today=TODAY, today_cost=0.05, budget=budget)
    assert normal == [], normal
