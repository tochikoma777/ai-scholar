"""L4：到期验证 / 命中率分母 / 样本不足标注。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlmodel import Session, SQLModel

from ai_scholar.config import AppSettings, Settings
from ai_scholar.pipeline.stats import calibration, impact, summarize, verify_due
from ai_scholar.store.db import engine_for
from ai_scholar.store.models import Paper, Signal
from ai_scholar.store.scoreboard import (
    Criterion,
    Opinion,
    Prediction,
    append_opinion,
    append_snapshot,
    opinions_path,
    scoreboard_path,
)


def tmp_settings(tmp_path: Path) -> Settings:
    return Settings(app=AppSettings(data_dir=str(tmp_path / "data")))


def make_paper(
    pid: str,
    *,
    first_seen: str = "2026-01-10",
    tier: int | None = None,
    citations: int | None = None,
    published: str = "2025-01-01T00:00:00Z",
) -> Paper:
    return Paper(
        id=pid,
        source="arxiv",
        primary_source="arxiv",
        first_seen_at=first_seen,
        published_at=published,
        title=f"Paper {pid}",
        abstract="A sufficiently long abstract. " * 10,
        authors_json="[]",
        primary_category="cs.AI",
        content_hash=f"h-{pid}",
        tier=tier,
        citation_count=citations,
    )


def seed(db_path: Path, papers: list[Paper], signals: list[Signal] | None = None) -> None:
    engine = engine_for(db_path)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for paper in papers:
            session.add(paper)
        for signal in signals or []:
            session.add(signal)
        session.commit()


def prediction(
    pid: str, criterion: Criterion, *, due: str = "2026-01-31", created: str = "2026-01-01"
) -> Prediction:
    return Prediction(
        id=pid,
        claim=pid,
        criterion=criterion,
        created_at=created,
        due_at=due,
    )


def test_verify_hit_miss_and_insufficient(tmp_path: Path) -> None:
    settings = tmp_settings(tmp_path)
    db_path = settings.db_path()
    seed(
        db_path,
        [
            make_paper("a", first_seen="2026-01-10"),
            make_paper("b", first_seen="2026-01-20"),
            make_paper("c", first_seen="2026-01-20"),
        ],
        [Signal(paper_id="c", observed_at="2026-01-15", citation_count=10, source="s2")],
    )
    path = scoreboard_path(settings.data_dir())
    append_snapshot(
        path,
        prediction(
            "p-hit",
            Criterion(type="arxiv_paper_count", target="cs.AI", threshold=2, window_days=30),
        ),
    )
    append_snapshot(
        path,
        prediction(
            "p-miss",
            Criterion(type="arxiv_paper_count", target="cs.AI", threshold=9, window_days=30),
        ),
    )
    append_snapshot(
        path,
        prediction(
            "p-cite", Criterion(type="citation_reach", target="c", threshold=5, window_days=30)
        ),
    )
    append_snapshot(
        path,
        prediction(
            "p-insufficient",
            Criterion(type="citation_reach", target="a", threshold=1, window_days=30),
        ),
    )

    report = verify_due(settings=settings, today=date(2026, 2, 1))
    assert (report.checked, report.hit, report.miss, report.insufficient) == (4, 2, 1, 1)

    # 幂等：已结案的不再验证
    again = verify_due(settings=settings, today=date(2026, 2, 1))
    assert again.checked == 0


def test_hit_rate_excludes_insufficient_and_opinions(tmp_path: Path) -> None:
    settings = tmp_settings(tmp_path)
    db_path = settings.db_path()
    seed(db_path, [make_paper("a")])
    path = scoreboard_path(settings.data_dir())
    append_snapshot(
        path,
        prediction(
            "p1", Criterion(type="arxiv_paper_count", target="cs.AI", threshold=1, window_days=30)
        ).model_copy(update={"status": "hit"}),
    )
    append_snapshot(
        path,
        prediction(
            "p2", Criterion(type="arxiv_paper_count", target="cs.AI", threshold=2, window_days=30)
        ).model_copy(update={"status": "miss"}),
    )
    append_snapshot(
        path,
        prediction(
            "p3", Criterion(type="citation_reach", target="a", threshold=1, window_days=30)
        ).model_copy(update={"status": "insufficient"}),
    )
    append_opinion(
        opinions_path(settings.data_dir()),
        Opinion(id="o1", text="a trend", created_at="2026-01-01"),
    )

    summary = summarize(settings=settings, today=date(2026, 2, 1))
    assert summary.hit_rate == 0.5  # 1/2，insufficient 不进分母
    assert summary.insufficient == 1
    assert summary.opinions == 1


def test_calibration_refuses_conclusion_on_small_sample(tmp_path: Path) -> None:
    settings = tmp_settings(tmp_path)
    db_path = settings.db_path()
    seed(db_path, [make_paper("a", tier=1, citations=10), make_paper("b", tier=2, citations=1)])
    engine = engine_for(db_path)
    with Session(engine) as session:
        result = calibration(session, settings.calibration, today=date(2026, 6, 1))
    assert not result.enough
    assert "样本不足" in result.reason


def test_calibration_computes_spearman_when_enough(tmp_path: Path) -> None:
    settings = tmp_settings(tmp_path)
    db_path = settings.db_path()
    papers = [make_paper(f"t1-{i}", tier=1, citations=10) for i in range(25)]
    papers += [make_paper(f"t2-{i}", tier=2, citations=1) for i in range(25)]
    seed(db_path, papers)
    engine = engine_for(db_path)
    with Session(engine) as session:
        result = calibration(session, settings.calibration, today=date(2026, 6, 1))
    assert result.enough
    assert result.spearman is not None and result.spearman < 0  # Tier 数字越小，引用越高


def test_impact_marks_short_span_as_insufficient(tmp_path: Path) -> None:
    settings = tmp_settings(tmp_path)
    db_path = settings.db_path()
    seed(
        db_path,
        [make_paper("a")],
        [
            Signal(paper_id="a", observed_at="2026-01-01", citation_count=1, source="s2"),
            Signal(paper_id="a", observed_at="2026-01-10", citation_count=5, source="s2"),
        ],
    )
    engine = engine_for(db_path)
    with Session(engine) as session:
        report = impact(session, settings.impact)
    assert report.rows
    assert "样本不足" in report.rows[0].verdict
