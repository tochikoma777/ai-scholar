"""每日报告测试（阶段 1：聚合 / 渲染 / 幂等 / 空态 / json 同步）。"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from ai_scholar.config import WatchlistConfig, WatchlistGroup
from ai_scholar.pipeline.report import (
    DailyReport,
    build_daily_report,
    render_markdown,
    write_report,
)
from ai_scholar.store.models import LLMUsage, Paper, Run
from ai_scholar.utils.timeutil import day_window

TODAY = date(2026, 10, 24)


def _watchlist() -> WatchlistConfig:
    return WatchlistConfig(
        default_threshold=0.9,
        groups=[WatchlistGroup(name="vision", weight=1.5, keywords=["multimodal"])],
    )


def _engine(tmp_path: Path):
    db = tmp_path / "data" / "ai_scholar.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    return engine


def _paper(i: int, day: str, title: str, abstract: str = "abstract") -> Paper:
    return Paper(
        id=f"arXiv:2610.0000{i}",
        source="arxiv",
        primary_source="arxiv",
        first_seen_at=day,
        published_at=f"{day}T00:00:00Z",
        title=title,
        abstract=abstract,
        authors_json=json.dumps(["John Doe"]),
        arxiv_id=f"2610.0000{i}",
        primary_category="cs.AI",
        content_hash=f"h{i}",
        selected=False,
    )


def test_build_aggregates_today(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(_paper(1, "2026-10-24", "A multimodal paper"))
        session.add(_paper(2, "2026-10-24", "Plain paper"))
        session.add(_paper(3, "2026-10-23", "Old paper"))
        session.commit()

    with Session(engine) as session:
        report = build_daily_report(session, today=TODAY, watchlist=_watchlist())

    assert report.total_papers == 3
    assert report.new_today == 2
    assert len(report.watchlist_hits) == 1
    assert report.watchlist_hits[0].id == "arXiv:2610.00001"
    assert report.deep_analyzed == []  # L3 才填，本阶段恒空
    assert report.new_graded == []


def test_watchlist_hits_sorted_and_capped(tmp_path: Path) -> None:
    """★ 命中按分数 desc 排序，且截断到 top_n。"""
    wl = WatchlistConfig(
        default_threshold=0.5,
        groups=[
            WatchlistGroup(name="a", weight=1.0, keywords=["multimodal"]),
            WatchlistGroup(name="b", weight=2.0, keywords=["video"]),
        ],
    )
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(_paper(1, "2026-10-24", "Plain", abstract="multimodal"))  # 分 1.0
        session.add(_paper(2, "2026-10-24", "Video paper", abstract="video"))  # 分 2.0
        session.add(_paper(3, "2026-10-24", "Both", abstract="multimodal video"))  # 分 3.0
        session.commit()

    with Session(engine) as session:
        report = build_daily_report(session, today=TODAY, watchlist=wl, top_n=2)

    assert [h.id for h in report.watchlist_hits] == ["arXiv:2610.00003", "arXiv:2610.00002"]
    assert report.watchlist_hits[0].score == 3.0


def test_runs_filtered_by_today_and_degraded(tmp_path: Path) -> None:
    start, _end = day_window(TODAY)
    today_run = (start + timedelta(hours=1)).isoformat(timespec="seconds")
    yesterday = (start - timedelta(hours=1)).isoformat(timespec="seconds")

    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(
            Run(
                run_id="r1",
                started_at=today_run,
                mode="off",
                source="arxiv",
                fetched=10,
                new=8,
                degraded_json=json.dumps({"hf_daily": "timeout"}),
            )
        )
        session.add(Run(run_id="r2", started_at=yesterday, mode="off", source="arxiv"))
        session.commit()

    with Session(engine) as session:
        report = build_daily_report(session, today=TODAY, watchlist=_watchlist())

    assert len(report.runs) == 1  # 昨天的 run 被过滤掉
    assert report.runs[0].run_id == "r1"
    assert report.runs[0].degraded == ["timeout"]
    assert report.degraded == {"hf_daily": "timeout"}


def test_write_report_idempotent(tmp_path: Path) -> None:
    """同一份数据写两次，md 与 json 内容一致（红线 7：无生成时间戳）。"""
    report = DailyReport(date="2026-10-24", total_papers=1, new_today=0)
    out_dir = tmp_path / "reports"
    first = write_report(report, out_dir)
    second = write_report(report, out_dir)
    assert first.md_path.read_text(encoding="utf-8") == second.md_path.read_text(encoding="utf-8")
    assert first.json_path.read_text(encoding="utf-8") == second.json_path.read_text(
        encoding="utf-8"
    )


def test_empty_state_renders_placeholder(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session:
        report = build_daily_report(session, today=TODAY, watchlist=_watchlist())

    md = render_markdown(report)
    assert "# 2026-10-24" in md
    assert "今日无 watchlist 命中" in md
    assert "暂无（L3 实现后填充）" in md
    assert "今日无运行记录" in md


def test_json_roundtrip(tmp_path: Path) -> None:
    report = DailyReport(date="2026-10-24", total_papers=7, new_today=3)
    outcome = write_report(report, tmp_path / "reports")
    loaded = DailyReport.model_validate(json.loads(outcome.json_path.read_text(encoding="utf-8")))
    assert loaded == report


def test_runs_bad_degraded_json_does_not_crash(tmp_path: Path) -> None:
    """★ 坏 degraded_json 不崩溃，只标记"解析失败"（与 first_seen 坏行跳过同机理）。"""
    start, _end = day_window(TODAY)
    today_run = (start + timedelta(hours=1)).isoformat(timespec="seconds")

    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(Run(run_id="r1", started_at=today_run, mode="off", degraded_json="{not json"))
        session.commit()

    with Session(engine) as session:
        report = build_daily_report(session, today=TODAY, watchlist=_watchlist())

    assert report.runs[0].degraded == ["degraded_json 解析失败"]


def test_md_frontmatter_carries_schema_version(tmp_path: Path) -> None:
    report = DailyReport(date="2026-10-24")
    md = render_markdown(report)
    assert "date: 2026-10-24" in md
    assert "schema_version: 1" in md


def test_report_cost_cny_sums_llm_usage_of_local_day(tmp_path: Path) -> None:
    """L6-2：当日成本按本地日窗口汇总 llm_usage；窗口外与空表都不得算进来。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'c.db'}")
    SQLModel.metadata.create_all(engine)
    start, end = day_window(TODAY)
    with Session(engine) as session:
        session.add(
            LLMUsage(
                run_id="r-in",
                model="deepseek-v4-flash",
                tokens_in=100,
                tokens_out=100,
                purpose="enhance",
                created_at=start.isoformat(timespec="seconds"),
                cost_cny=0.1234,
                cache_hit=0,
            )
        )
        session.add(
            LLMUsage(
                run_id="r-out",
                model="deepseek-v4-flash",
                tokens_in=100,
                tokens_out=100,
                purpose="enhance",
                created_at=end.isoformat(timespec="seconds"),  # 次日 00:00 = 下一窗口
                cost_cny=9.9,
                cache_hit=0,
            )
        )
        session.commit()
        report = build_daily_report(session, today=TODAY, watchlist=_watchlist())

    assert report.cost_cny == 0.1234
    assert "当日实际：¥0.1234" in render_markdown(report)


def test_report_cost_cny_zero_without_usage(tmp_path: Path) -> None:
    """off 档（无 llm_usage 行）显示 ¥0，而不是 None / 「—」。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'c.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        report = build_daily_report(session, today=TODAY, watchlist=_watchlist())

    assert report.cost_cny == 0.0
    assert "当日实际：¥0.0000" in render_markdown(report)


def test_report_renders_predictions_without_merging_opinions(tmp_path: Path) -> None:
    from ai_scholar.store.scoreboard import (
        Criterion,
        Opinion,
        Prediction,
        append_opinion,
        append_snapshot,
        opinions_path,
        scoreboard_path,
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = scoreboard_path(data_dir)
    append_snapshot(
        path,
        Prediction(
            id="p-1",
            claim="cs.AI 未来 30 天新增 ≥ 10 篇",
            criterion=Criterion(
                type="arxiv_paper_count", target="cs.AI", threshold=10, window_days=30
            ),
            created_at="2026-10-01",
            due_at="2026-10-24",
            status="hit",
            verified_at="2026-10-24",
            observed=12,
        ),
    )
    append_snapshot(
        path,
        Prediction(
            id="p-2",
            claim="cs.LG 未来 30 天新增 ≥ 99 篇",
            criterion=Criterion(
                type="arxiv_paper_count", target="cs.LG", threshold=99, window_days=30
            ),
            created_at="2026-10-01",
            due_at="2026-10-24",
        ),
    )
    append_snapshot(
        path,
        Prediction(
            id="p-3",
            claim="cs.CV 未来 30 天新增 ≥ 5 篇",
            criterion=Criterion(
                type="arxiv_paper_count", target="cs.CV", threshold=5, window_days=30
            ),
            created_at="2026-10-24",
            due_at="2026-11-23",
        ),
    )
    append_opinion(
        opinions_path(data_dir), Opinion(id="o-1", text="a trend", created_at="2026-10-24")
    )

    engine = _engine(tmp_path)
    with Session(engine) as session:
        daily = build_daily_report(session, today=TODAY, watchlist=_watchlist(), data_dir=data_dir)

    assert [item.id for item in daily.predictions_verified] == ["p-1"]
    assert [item.id for item in daily.predictions_due] == ["p-2"]
    assert [item.id for item in daily.predictions_new] == ["p-3"]
    assert daily.opinions_total == 1
    md = render_markdown(daily)
    assert "今日推演" in md
    assert "观测 12 / 阈值 10" in md
    assert "🆕" in md
    assert "观点 1 条" in md


# ── L8-F：成本异常只标注不阻断 ──────────────────────────────────────────
def _report_with_cost(tmp_path: Path, cost: float, *, daily_limit: float) -> DailyReport:
    from ai_scholar.budget import BudgetSettings

    engine = _engine(tmp_path)
    start, _end = day_window(TODAY)
    with Session(engine) as session:
        session.add(
            LLMUsage(
                run_id="r-cost",
                model="deepseek-v4-pro",
                tokens_in=100,
                tokens_out=100,
                purpose="deep_analysis",
                created_at=start.isoformat(timespec="seconds"),
                cost_cny=cost,
                cache_hit=0,
            )
        )
        session.commit()
        return build_daily_report(
            session,
            today=TODAY,
            watchlist=_watchlist(),
            data_dir=tmp_path / "data",
            budget=BudgetSettings(daily_limit_cny=daily_limit, monthly_limit_cny=1000.0),
        )


def test_cost_anomaly_is_flagged_in_report(tmp_path: Path) -> None:
    """★ 超日限额一半 ⇒ 报告字段 + md 顶部都要有警示（但仍能正常生成）。"""
    report = _report_with_cost(tmp_path, cost=0.8, daily_limit=1.0)
    assert report.cost_anomalies, "应命中『今日 > 日限额一半』"
    md = render_markdown(report)
    assert "成本异常" in md
    assert "日限额一半" in md


def test_cost_anomaly_absent_when_normal(tmp_path: Path) -> None:
    report = _report_with_cost(tmp_path, cost=0.05, daily_limit=10.0)
    assert report.cost_anomalies == []
    assert "成本异常" not in render_markdown(report)


def test_cost_anomaly_not_in_degraded(tmp_path: Path) -> None:
    """★ 成本异常与"源故障降级"是两回事，别混在一个字段里。"""
    report = _report_with_cost(tmp_path, cost=0.8, daily_limit=1.0)
    assert "cost" not in report.degraded
