"""L3b-2：深度分析管道测试。"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from sqlmodel import Session, SQLModel, select

from ai_scholar.budget import BudgetSettings
from ai_scholar.config import LLMSettings, Settings
from ai_scholar.llm import LLMResponse
from ai_scholar.models import WatchlistConfig, WatchlistGroup
from ai_scholar.pipeline.deep_analysis import run_deep_analysis
from ai_scholar.pipeline.report import build_daily_report
from ai_scholar.store.db import engine_for
from ai_scholar.store.models import LLMUsage, Paper

DAY = date(2026, 10, 24)
WATCHLIST = WatchlistConfig(
    default_threshold=0.9,
    groups=[WatchlistGroup(name="agents", weight=1.0, keywords=["retrieval"])],
)


def strong_settings(**budget_overrides: object) -> Settings:
    prices = {
        "deepseek": {
            "deepseek-v4-pro": {
                "input_cached_cny_per_1m": 0.025,
                "input_miss_cny_per_1m": 3.0,
                "output_cny_per_1m": 6.0,
            },
            "deepseek-v4-flash": {
                "input_cached_cny_per_1m": 0.02,
                "input_miss_cny_per_1m": 1.0,
                "output_cny_per_1m": 2.0,
            },
        }
    }
    budget = BudgetSettings(prices=prices, **budget_overrides)
    return Settings(
        llm=LLMSettings(api_key="k", base_url="https://api.deepseek.com"), budget=budget
    )


def make_paper(
    pid: str,
    *,
    title: str = "Paper",
    abstract: str = "retrieval augmented generation",
    tier: int | None = None,
    arxiv_id: str | None = None,
) -> Paper:
    return Paper(
        id=pid,
        source="arxiv",
        primary_source="arxiv",
        first_seen_at="2026-10-24",
        published_at="2026-10-20T00:00:00Z",
        title=title,
        abstract=abstract,
        authors_json="[]",
        venue=None,
        url=f"https://arxiv.org/abs/{arxiv_id or pid}",
        arxiv_id=arxiv_id,
        doi=None,
        primary_category="cs.AI",
        categories_json="[]",
        citation_count=1,
        tier=tier,
        content_hash=f"h-{pid}",
    )


def seed(db_path: Path, papers: list[Paper], usage: list[LLMUsage] | None = None) -> None:
    engine = engine_for(db_path)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for paper in papers:
            session.add(paper)
        for row in usage or []:
            session.add(row)
        session.commit()


class FakeDeepLLM:
    def __init__(self, *, echo_id: bool = True):
        self.echo_id = echo_id
        self.calls = 0
        self.last_prompt = ""

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict | None = None,
        json_mode: bool = True,
    ) -> LLMResponse:
        self.calls += 1
        text = messages[0]["content"]
        self.last_prompt = text
        match = re.search(r"论文 ID：`([^`]+)`", text)
        pid = match.group(1) if match else "unknown"
        body = "## TL;DR\n坐标：演进｜痛点：…\n## 六段\n### 总体介绍\n…"
        if self.echo_id:
            body = f"{pid}\n{body}"
        return LLMResponse(content=body, tokens_in=1000, tokens_out=500, cache_hit=False)


def load_papers(db_path: Path) -> dict[str, Paper]:
    engine = engine_for(db_path)
    with Session(engine) as session:
        return {p.id: p for p in session.exec(select(Paper)).all()}


async def test_off_mode_returns_zero_calls() -> None:
    settings = Settings(llm=LLMSettings(api_key="", base_url=""))
    report = await run_deep_analysis(settings=settings, today=DAY)
    assert report.mode == "off"
    assert report.llm_calls == 0


async def test_channel_a_and_b_selection(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(
        db,
        [
            make_paper("arXiv:A", title="RAG Paper"),
            make_paper("arXiv:B", title="Tier One", abstract="unrelated", tier=1),
            make_paper("arXiv:C", title="Other", abstract="unrelated"),
        ],
    )
    fake = FakeDeepLLM()
    report = await run_deep_analysis(
        settings=strong_settings(),
        db_path=db,
        client=fake,
        watchlist=WATCHLIST,
        today=DAY,
    )
    assert report.requested == 2  # A（通道 A）+ B（通道 B）
    assert report.analyzed == 2
    papers = load_papers(db)
    assert papers["arXiv:A"].deep_status == "done"
    assert papers["arXiv:B"].deep_status == "done"
    assert papers["arXiv:C"].deep_analysis_md is None


async def test_weekly_quota_blocks(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    rows = [
        LLMUsage(
            run_id=f"r{i}",
            model="deepseek-v4-pro",
            tokens_in=1,
            tokens_out=1,
            cost_cny=0.001,
            purpose="deep_analysis",
            created_at="2026-10-23T12:00:00+00:00",
        )
        for i in range(5)
    ]
    seed(db, [make_paper("arXiv:A")], usage=rows)
    fake = FakeDeepLLM()
    report = await run_deep_analysis(
        settings=strong_settings(),
        db_path=db,
        client=fake,
        watchlist=WATCHLIST,
        today=DAY,
    )
    assert report.blocked == 1
    assert report.llm_calls == 0
    assert "quota" in report.degraded


async def test_idempotent_and_force(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A")])
    settings = strong_settings()
    await run_deep_analysis(
        settings=settings, db_path=db, client=FakeDeepLLM(), watchlist=WATCHLIST, today=DAY
    )

    fake2 = FakeDeepLLM()
    report = await run_deep_analysis(
        settings=settings, db_path=db, client=fake2, watchlist=WATCHLIST, today=DAY
    )
    assert report.requested == 0
    assert report.skipped == 1
    assert fake2.calls == 0

    fake3 = FakeDeepLLM()
    report3 = await run_deep_analysis(
        settings=settings,
        db_path=db,
        client=fake3,
        watchlist=WATCHLIST,
        today=DAY,
        force=True,
    )
    assert report3.analyzed == 1 and fake3.calls == 1


async def test_failure_marks_failed_without_hash(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A")])
    report = await run_deep_analysis(
        settings=strong_settings(),
        db_path=db,
        client=FakeDeepLLM(echo_id=False),
        watchlist=WATCHLIST,
        today=DAY,
    )
    assert report.analyzed == 0
    assert report.llm_calls == 1  # 真实调用即记账
    assert "llm" in report.degraded
    paper = load_papers(db)["arXiv:A"]
    assert paper.deep_status == "failed"
    assert paper.deep_content_hash is None


async def test_fulltext_is_used_and_not_persisted(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A", arxiv_id="2610.00001")])

    async def fake_fetch(http, arxiv_id, *, max_chars=60000):
        return "FULLTEXT BODY"

    monkeypatch.setattr("ai_scholar.pipeline.deep_analysis.fetch_arxiv_html", fake_fetch)
    fake = FakeDeepLLM()
    report = await run_deep_analysis(
        settings=strong_settings(),
        db_path=db,
        client=fake,
        watchlist=WATCHLIST,
        today=DAY,
    )
    assert report.fulltext_hits == 1
    assert "FULLTEXT BODY" in fake.last_prompt
    # ★ 正文不进库：只存分析 Markdown
    paper = load_papers(db)["arXiv:A"]
    assert "FULLTEXT BODY" not in (paper.deep_analysis_md or "")


async def test_report_deep_analyzed_filled(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A", title="Analyzed Paper")])
    await run_deep_analysis(
        settings=strong_settings(),
        db_path=db,
        client=FakeDeepLLM(),
        watchlist=WATCHLIST,
        today=DAY,
    )
    engine = engine_for(db)
    with Session(engine) as session:
        report = build_daily_report(session, today=DAY, watchlist=WATCHLIST)
    assert any(hit.id == "arXiv:A" for hit in report.deep_analyzed)
