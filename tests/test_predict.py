"""L4：可证伪门禁 + 推演生成（★ 确定性、幂等、观点不进记分板）。"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from sqlmodel import Session, SQLModel

from ai_scholar.config import AppSettings, Settings
from ai_scholar.pipeline.predict import run_predict
from ai_scholar.store.db import engine_for
from ai_scholar.store.models import Paper
from ai_scholar.store.scoreboard import (
    Criterion,
    Prediction,
    append_snapshot,
    falsifiability_errors,
    load_latest,
    load_opinions,
    opinions_path,
    scoreboard_path,
)


def tmp_settings(tmp_path: Path) -> Settings:
    return Settings(app=AppSettings(data_dir=str(tmp_path / "data")))


def make_paper(
    pid: str,
    *,
    first_seen: str = "2026-03-10",
    citations: int | None = None,
    tags: list[str] | None = None,
    published: str = "2025-12-01T00:00:00Z",
) -> Paper:
    return Paper(
        id=pid,
        source="arxiv",
        primary_source="arxiv",
        first_seen_at=first_seen,
        published_at=published,
        title=f"Paper {pid} about KV cache",
        abstract="A sufficiently long abstract. " * 10,
        authors_json="[]",
        primary_category="cs.AI",
        categories_json='["cs.AI"]',
        content_hash=f"h-{pid}",
        citation_count=citations,
        method_tags_json=json.dumps(tags) if tags is not None else None,
    )


def seed(db_path: Path, papers: list[Paper]) -> None:
    engine = engine_for(db_path)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for paper in papers:
            session.add(paper)
        session.commit()


def test_falsifiability_is_decided_by_code_not_by_claim() -> None:
    today = date(2026, 3, 31)
    good = Criterion(type="arxiv_paper_count", target="cs.AI", threshold=10, window_days=30)
    assert falsifiability_errors(good, due_at="2026-04-30", today=today) == []

    unknown = Criterion(type="vibes", target="agents", threshold=1, window_days=30)
    assert falsifiability_errors(unknown, due_at="2026-04-30", today=today)
    assert falsifiability_errors(good, due_at="2026-03-31", today=today)  # 到期日不晚于今天
    zero = Criterion(type="entity_mentions", target="KV cache", threshold=0, window_days=30)
    assert falsifiability_errors(zero, due_at="2026-04-30", today=today)


def test_predict_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    settings = tmp_settings(tmp_path)
    db_path = settings.db_path()
    papers = [make_paper(f"arXiv:{i:05d}", citations=12) for i in range(40)]
    papers += [
        make_paper(f"arXiv:9{i:04d}", tags=["KV cache", "speculative decoding"]) for i in range(10)
    ]
    seed(db_path, papers)

    first = run_predict(settings=settings, db_path=db_path, today=date(2026, 3, 31), limit=5)
    assert first.created == 5
    stored = load_latest(scoreboard_path(settings.data_dir()))
    assert len(stored) == 5
    assert all(item.status == "open" for item in stored)
    assert all(
        item.criterion.type in {"arxiv_paper_count", "entity_mentions", "citation_reach"}
        for item in stored
    )

    second = run_predict(settings=settings, db_path=db_path, today=date(2026, 3, 31), limit=5)
    assert second.created == 0
    assert second.skipped >= 5

    # dry-run 不落盘
    before = scoreboard_path(settings.data_dir()).read_text(encoding="utf-8")
    dry = run_predict(
        settings=settings, db_path=db_path, today=date(2026, 3, 31), limit=5, dry_run=True
    )
    assert dry.created >= 0
    assert scoreboard_path(settings.data_dir()).read_text(encoding="utf-8") == before


def test_opinion_never_enters_scoreboard(tmp_path: Path) -> None:
    settings = tmp_settings(tmp_path)
    db_path = settings.db_path()
    seed(db_path, [make_paper("arXiv:00001")])

    report = run_predict(
        settings=settings,
        db_path=db_path,
        today=date(2026, 3, 31),
        insights=["智能体瓶颈已从模型转向脚手架"],
    )
    assert report.opinions == 1
    opinions = load_opinions(opinions_path(settings.data_dir()))
    assert [item.text for item in opinions] == ["智能体瓶颈已从模型转向脚手架"]
    assert load_latest(scoreboard_path(settings.data_dir())) == []


def test_missing_db_is_explicit_degradation(tmp_path: Path) -> None:
    settings = tmp_settings(tmp_path)
    report = run_predict(
        settings=settings,
        db_path=settings.db_path(),
        today=date(2026, 3, 31),
    )
    assert report.created == 0
    assert "db" in report.degraded


def test_verified_prediction_is_not_regenerated(tmp_path: Path) -> None:
    settings = tmp_settings(tmp_path)
    criterion = Criterion(type="arxiv_paper_count", target="cs.AI", threshold=1, window_days=30)
    closed = Prediction(
        id="p-closed",
        claim="closed",
        criterion=criterion,
        created_at="2026-01-01",
        due_at="2026-01-31",
        status="hit",
        observed=3,
        verified_at="2026-02-01",
    )
    assert append_snapshot(scoreboard_path(settings.data_dir()), closed)

    db_path = settings.db_path()
    seed(db_path, [make_paper("arXiv:00002")])
    report = run_predict(settings=settings, db_path=db_path, today=date(2026, 3, 31))
    # 已结案的同目标不阻止新预测（新基线可以再预测），但旧快照不会被改写
    assert load_latest(scoreboard_path(settings.data_dir()))[0].status == "hit"
    assert report.created >= 0


def test_created_prediction_due_date_matches_horizon(tmp_path: Path) -> None:
    settings = tmp_settings(tmp_path)
    db_path = settings.db_path()
    seed(db_path, [make_paper(f"arXiv:{i:05d}") for i in range(40)])
    today = date(2026, 3, 31)
    run_predict(settings=settings, db_path=db_path, today=today, limit=1)
    item = load_latest(scoreboard_path(settings.data_dir()))[0]
    assert item.due_at == (today + timedelta(days=item.criterion.window_days)).isoformat()
