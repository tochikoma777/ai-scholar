"""L1-B：预筛 + 采样状态机（★ 时序驱动，最容易"当天对、跨天错"）。

这里全部用**受控时钟**（显式传 `today`）推进，不用 `sleep`、不依赖真实日期。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlmodel import Session, select
from typer.testing import CliRunner

from ai_scholar.cli import app
from ai_scholar.config import SignalsSettings, load_settings
from ai_scholar.pipeline.collect import run_collect
from ai_scholar.pipeline.prescreen import apply_prescreen, prescreen, should_retain
from ai_scholar.pipeline.signals import advance, run_signals
from ai_scholar.store.db import engine_for
from ai_scholar.store.models import (
    SIGNAL_DORMANT,
    SIGNAL_PENDING,
    SIGNAL_READY,
    SIGNAL_REJECTED,
    SIGNAL_SAMPLE,
    SIGNAL_TRACKED,
    Paper,
    Signal,
)
from tests.test_collect import arxiv_entry, atom_feed

S2_URL = "https://s2.test/graph/v1/paper/batch"
runner = CliRunner()
CFG = SignalsSettings()


def make_paper(
    pid: str = "arXiv:2610.00001",
    *,
    first_seen: str = "2026-01-01",
    published: str = "2025-12-25T00:00:00Z",
    abstract: str = "A sufficiently long abstract. " * 20,
    state: str | None = SIGNAL_PENDING,
    next_at: str | None = "2026-01-01",
) -> Paper:
    return Paper(
        id=pid,
        source="arxiv",
        primary_source="arxiv",
        first_seen_at=first_seen,
        published_at=published,
        title="A Study of Things",
        abstract=abstract,
        authors_json="[]",
        venue=None,
        url="https://arxiv.org/abs/2610.00001",
        arxiv_id=pid.split(":", 1)[-1],
        doi=None,
        primary_category="cs.AI",
        categories_json="[]",
        content_hash=f"h-{pid}",
        signal_state=state,
        signal_next_at=next_at,
    )


def seed(db_path: Path, papers: list[Paper]) -> None:
    engine = engine_for(db_path)
    from sqlmodel import SQLModel

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for paper in papers:
            session.add(paper)
        session.commit()


def s2_payload(arxiv: str, *, citations: int = 5, refs: int = 10) -> dict:
    return {
        "paperId": f"s2-{arxiv}",
        "externalIds": {"ArXiv": arxiv},
        "citationCount": citations,
        "referenceCount": refs,
        "references": [],
    }


def mock_s2(mapping: dict[str, dict | None]) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["ids"]
        return httpx.Response(200, json=[mapping.get(i) for i in ids])

    return respx.post(S2_URL).mock(side_effect=handler)


def settings_for(tmp_path: Path) -> object:
    settings = load_settings()
    settings.app.data_dir = str(tmp_path / "data")
    cfg = settings.source("semantic_scholar")
    assert cfg is not None
    cfg.endpoint = S2_URL
    cfg.api_key = ""
    return settings


# ── ① 状态机：pending ───────────────────────────────────────────────────
def test_pending_samples_monthly_before_window_expires() -> None:
    paper = make_paper()
    event = advance(paper, today=date(2026, 1, 31), cfg=CFG, citations=0)
    assert event == "sampled"
    assert paper.signal_state == SIGNAL_PENDING
    assert paper.signal_next_at == (date(2026, 1, 31) + timedelta(days=30)).isoformat()


def test_pending_goes_dormant_after_90_days() -> None:
    """★ 观测窗口用 first_seen：第 90 天仍未成熟 ⇒ dormant。"""
    paper = make_paper()
    today = date(2026, 1, 1) + timedelta(days=90)
    event = advance(paper, today=today, cfg=CFG, citations=0)
    assert event == "dormant"
    assert paper.signal_state == SIGNAL_DORMANT
    # 复活点 = published_at + 180（不是 first_seen + 180）
    assert paper.signal_next_at == (date(2025, 12, 25) + timedelta(days=180)).isoformat()


def test_mature_paper_skips_pending_via_fast_path() -> None:
    """★ 成熟度用 published_at：≥180 天 ⇒ ready（快路径，跳过 pending）。"""
    paper = make_paper(first_seen="2026-01-01", published="2025-06-01T00:00:00Z")
    event = advance(paper, today=date(2026, 1, 2), cfg=CFG, citations=0)
    assert event == "ready"
    assert paper.signal_state == SIGNAL_READY
    assert paper.signal_next_at is None


# ── ② dormant 复活（★ 条件只含 citations，最多一次）──────────────────────
def test_dormant_revives_when_citations_reach_threshold() -> None:
    paper = make_paper(published="2025-01-01T00:00:00Z", state=SIGNAL_DORMANT, next_at="2025-06-30")
    event = advance(paper, today=date(2025, 7, 1), cfg=CFG, citations=CFG.min_citations)
    assert event == "revived"
    assert paper.signal_state == SIGNAL_PENDING
    assert paper.signal_revived is True


def test_dormant_stays_dead_without_citations() -> None:
    paper = make_paper(published="2025-01-01T00:00:00Z", state=SIGNAL_DORMANT, next_at="2025-06-30")
    event = advance(paper, today=date(2025, 7, 1), cfg=CFG, citations=CFG.min_citations - 1)
    assert event == "dormant_final"
    assert paper.signal_state == SIGNAL_DORMANT
    assert paper.signal_next_at is None


def test_revival_condition_ignores_age() -> None:
    """★ age 只决定"何时采"：论文很老但 0 引用 ⇒ 不得因 age 复活。"""
    paper = make_paper(
        published="2020-01-01T00:00:00Z",  # 5 年前
        state=SIGNAL_DORMANT,
        next_at="2020-06-29",
    )
    event = advance(paper, today=date(2026, 10, 24), cfg=CFG, citations=0)
    assert event == "dormant_final"
    assert paper.signal_state == SIGNAL_DORMANT


def test_revival_happens_at_most_once() -> None:
    """★ 复活额度用掉后，即使再次满足条件也不再采样。"""
    paper = make_paper(published="2025-01-01T00:00:00Z", state=SIGNAL_DORMANT, next_at="2025-06-30")
    advance(paper, today=date(2025, 7, 1), cfg=CFG, citations=0)  # 第一次：不达标
    assert paper.signal_revived is True

    event = advance(paper, today=date(2025, 8, 1), cfg=CFG, citations=999)
    assert event == "dormant_final"
    assert paper.signal_state == SIGNAL_DORMANT
    assert paper.signal_next_at is None


# ── ③ tracked / sample 的频率与保留期 ───────────────────────────────────
def test_tracked_is_monthly_then_quarterly() -> None:
    paper = make_paper(state=SIGNAL_TRACKED, next_at="2026-03-01")
    advance(paper, today=date(2026, 3, 1), cfg=CFG, citations=1)
    assert paper.signal_next_at == (date(2026, 3, 1) + timedelta(days=30)).isoformat()

    paper2 = make_paper(first_seen="2026-01-01", state=SIGNAL_TRACKED, next_at="2027-06-01")
    advance(paper2, today=date(2027, 6, 1), cfg=CFG, citations=1)
    assert paper2.signal_next_at == (date(2027, 6, 1) + timedelta(days=91)).isoformat()


def test_tracked_stops_after_retention() -> None:
    paper = make_paper(first_seen="2020-01-01", state=SIGNAL_TRACKED, next_at="2026-01-01")
    event = advance(paper, today=date(2026, 1, 1), cfg=CFG, citations=1)
    assert event == "retired"
    assert paper.signal_next_at is None


def test_sample_expires_after_one_year() -> None:
    paper = make_paper(first_seen="2025-01-01", state=SIGNAL_SAMPLE, next_at="2026-01-01")
    event = advance(paper, today=date(2026, 1, 5), cfg=CFG, citations=0)
    assert event == "sample_expired"
    assert paper.signal_next_at is None


# ── ④ 预筛 ──────────────────────────────────────────────────────────────
def test_prescreen_rejects_short_abstract() -> None:
    paper = make_paper(abstract="too short")
    passed, reason = prescreen(paper, load_settings().prescreen)
    assert passed is False
    assert reason and "摘要过短" in reason


def test_prescreen_rejects_retraction_title() -> None:
    paper = make_paper()
    paper.title = "Retraction Note: A Study of Things"
    passed, reason = prescreen(paper, load_settings().prescreen)
    assert passed is False
    assert reason and "排除词" in reason


def test_prescreen_passes_normal_paper() -> None:
    passed, reason = prescreen(make_paper(), load_settings().prescreen)
    assert passed is True
    assert reason is None


def test_retain_ratio_applies_to_rejected_not_overall() -> None:
    """★ r=0.2 是**淘汰集**留存率：只有被淘汰的才抽样，通过的不受影响。"""
    ids = [f"arXiv:2610.{i:05d}" for i in range(2000)]
    kept = [pid for pid in ids if should_retain(pid, 0.2)]
    assert 0.15 < len(kept) / len(ids) < 0.25  # 确定性哈希，比例应贴近 20%


def test_apply_prescreen_routes_rejected_to_sample_or_rejected() -> None:
    cfg = load_settings()
    rejected_papers = [make_paper(pid=f"arXiv:2610.{i:05d}", abstract="short") for i in range(300)]
    states = set()
    for paper in rejected_papers:
        passed = apply_prescreen(
            paper, prescreen_cfg=cfg.prescreen, signals_cfg=cfg.signals, today=date(2026, 1, 1)
        )
        assert passed is False
        states.add(paper.signal_state)
    assert states == {SIGNAL_SAMPLE, SIGNAL_REJECTED}


def test_prescreen_does_not_decide_ingestion(tmp_path: Path) -> None:
    """★ 预筛淘汰的论文**照样入库**（只影响是否跟踪）。"""
    settings = settings_for(tmp_path)
    db_path = tmp_path / "data" / "t.db"
    short_abstract = "short"
    feed = atom_feed(arxiv_entry(arxiv_id="2610.90001v1", abstract=short_abstract), total=1)

    async def run() -> None:
        with respx.mock:
            respx.get("https://export.arxiv.org/api/query").mock(
                return_value=httpx.Response(200, text=feed)
            )
            await run_collect(
                days=1,
                source="arxiv",
                settings=settings,
                db_path=db_path,
                first_seen_path=tmp_path / "first_seen.jsonl",
                today=date(2026, 1, 1),
            )

    import asyncio

    asyncio.run(run())

    with Session(engine_for(db_path)) as session:
        paper = session.get(Paper, "arXiv:2610.90001")
        assert paper is not None, "★ 预筛不得阻止入库"
        assert paper.signal_state in {SIGNAL_REJECTED, SIGNAL_SAMPLE}
        assert paper.prescreen_reason and "摘要过短" in paper.prescreen_reason


# ── ⑤ signals 写入幂等 + JSONL ──────────────────────────────────────────
@respx.mock
async def test_signals_row_is_idempotent_per_day(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    seed(
        db_path,
        [
            make_paper(
                pid="arXiv:2610.00001",
                state=SIGNAL_PENDING,
                next_at="2026-03-01",
            )
        ],
    )
    mock_s2({"ARXIV:2610.00001": s2_payload("2610.00001", citations=9)})
    settings = settings_for(tmp_path)
    day = date(2026, 3, 1)

    first = await run_signals(today=day, settings=settings, db_path=db_path)
    assert first.rows_new == 1
    assert first.fetched == 1

    # ★ 强制重跑同一天：不得产生第二行
    second = await run_signals(today=day, force=True, settings=settings, db_path=db_path)
    assert second.rows_new == 0
    assert second.rows_existing == 1

    with Session(engine_for(db_path)) as session:
        rows = list(session.exec(select(Signal)).all())
        assert len(rows) == 1
        assert rows[0].citation_count == 9
        assert rows[0].observed_at == "2026-03-01"

    jsonl = Path(settings.data_dir()) / "signals" / "2026-03.jsonl"
    assert len(jsonl.read_text(encoding="utf-8").strip().splitlines()) == 1


@respx.mock
async def test_signals_advances_state_on_same_run(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    seed(db_path, [make_paper(state=SIGNAL_PENDING, next_at="2026-03-01")])
    mock_s2({"ARXIV:2610.00001": s2_payload("2610.00001", citations=0)})
    settings = settings_for(tmp_path)
    report = await run_signals(today=date(2026, 3, 1), settings=settings, db_path=db_path)
    assert report.events.get("sampled") == 1
    with Session(engine_for(db_path)) as session:
        paper = session.get(Paper, "arXiv:2610.00001")
        assert paper.signal_next_at == (date(2026, 3, 1) + timedelta(days=30)).isoformat()
        assert paper.signal_last_at == "2026-03-01"


# ── ⑥ CLI: signals / mark ───────────────────────────────────────────────
@respx.mock
def test_signals_cli_reports_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    db_path = root / "data" / "ai_scholar.db"
    seed(db_path, [make_paper(state=SIGNAL_PENDING, next_at="2026-03-01")])
    mock_s2({"ARXIV:2610.00001": s2_payload("2610.00001", citations=4)})
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(root))
    monkeypatch.setenv("AI_SCHOLAR_SOURCES__SEMANTIC_SCHOLAR__ENDPOINT", S2_URL)

    result = runner.invoke(app, ["signals"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "signals 新增行" in result.output
    assert "状态分布" in result.output


@respx.mock
async def test_signals_skips_paper_already_in_jsonl(tmp_path: Path) -> None:
    """★ 真源幂等：JSONL 已有该 paper 当日行，即使 db `signals` 表为空也不重复追加。

    场景：db 是可丢弃缓存，丢弃后 `signals` 表空、但 JSONL 真源仍在。
    若只查 db 会重新采样、重复污染 JSONL（2026-10-24 实跑踩中）。
    """
    db_path = tmp_path / "data" / "t.db"
    seed(
        db_path,
        [make_paper(pid="arXiv:2610.00001", state=SIGNAL_PENDING, next_at="2026-03-01")],
    )
    mock_s2({"ARXIV:2610.00001": s2_payload("2610.00001", citations=9)})
    settings = settings_for(tmp_path)
    day = date(2026, 3, 1)

    jsonl = Path(settings.data_dir()) / "signals" / "2026-03.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        json.dumps(
            {
                "paper_id": "arXiv:2610.00001",
                "observed_at": "2026-03-01",
                "citation_count": 9,
                "reference_count": 10,
                "source": "s2",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = await run_signals(today=day, settings=settings, db_path=db_path)

    assert report.rows_new == 0  # ★ JSONL 已有 → 不重复追加
    assert report.rows_existing == 1

    lines = jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1  # 真源不被污染

    # ★ advance 仍推进 signal_next_at（避免一直 due）
    with Session(engine_for(db_path)) as session:
        paper = session.get(Paper, "arXiv:2610.00001")
        assert paper.signal_next_at == (day + timedelta(days=30)).isoformat()
