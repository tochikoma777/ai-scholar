"""L3b-1：文献定级测试。

★ 用注入的 FakeGradeLLM 做管道级断言；off 档断言零调用。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from sqlmodel import Session, SQLModel, select

from ai_scholar.budget import BudgetSettings
from ai_scholar.config import LLMSettings, Settings
from ai_scholar.llm import LLMResponse
from ai_scholar.models import WatchlistConfig
from ai_scholar.pipeline.grade import parse_grade, run_grade
from ai_scholar.pipeline.report import build_daily_report
from ai_scholar.store.db import engine_for
from ai_scholar.store.models import LLMUsage, Paper

DAY = date(2026, 10, 24)


def fast_settings(**budget_overrides: object) -> Settings:
    prices = {
        "deepseek": {
            "deepseek-v4-flash": {
                "input_cached_cny_per_1m": 0.02,
                "input_miss_cny_per_1m": 1.0,
                "output_cny_per_1m": 2.0,
            }
        }
    }
    budget = BudgetSettings(prices=prices, **budget_overrides)
    return Settings(
        llm=LLMSettings(api_key="k", base_url="https://api.deepseek.com"), budget=budget
    )


def make_paper(
    pid: str,
    *,
    published: str = "2025-01-01T00:00:00Z",
    citations: int = 0,
    title: str = "Paper",
) -> Paper:
    return Paper(
        id=pid,
        source="arxiv",
        primary_source="arxiv",
        first_seen_at="2026-10-24",
        published_at=published,
        title=title,
        abstract="abstract " * 40,
        authors_json="[]",
        venue=None,
        url=f"https://arxiv.org/abs/{pid}",
        arxiv_id=None,
        doi=None,
        primary_category="cs.AI",
        categories_json="[]",
        citation_count=citations,
        content_hash=f"h-{pid}",
    )


def seed(db_path: Path, papers: list[Paper]) -> None:
    engine = engine_for(db_path)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for paper in papers:
            session.add(paper)
        session.commit()


class FakeGradeLLM:
    def __init__(self, *, tier: int = 1, confidence: str = "高", echo_id: bool = True):
        self.tier = tier
        self.confidence = confidence
        self.echo_id = echo_id
        self.calls = 0

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict | None = None,
        json_mode: bool = True,
    ) -> LLMResponse:
        self.calls += 1
        text = messages[0]["content"]
        match = re.search(r"论文 ID：`([^`]+)`", text)
        pid = match.group(1) if match else "unknown"
        body = f"【定级表】\n→ 高×2 ⇒ Tier {self.tier}\n【置信度】{self.confidence}"
        if self.echo_id:
            body = f"{pid}\n{body}"
        return LLMResponse(content=body, tokens_in=100, tokens_out=50, cache_hit=False)


def load_papers(db_path: Path) -> dict[str, Paper]:
    engine = engine_for(db_path)
    with Session(engine) as session:
        return {p.id: p for p in session.exec(select(Paper)).all()}


def test_parse_grade() -> None:
    assert parse_grade("→ 高×2 ⇒ Tier 1\n【置信度】高") == (1, "高")
    assert parse_grade("Tier 3") == (3, None)
    assert parse_grade("没有结构化结果") == (None, None)


def test_parse_grade_truncated_table_returns_none() -> None:
    """★ L6-4 实测回归：max_tokens 被推理 token 吃光时，答案会停在定级表表头。

    该形态必须解析失败（返回 None）而不是蒙一个 Tier ——
    `grade.py` 靠"解析失败 ⇒ 不写指纹 ⇒ 下次重试"来自愈，
    修法是把 `max_tokens_fast` 抬到 8000（2026-10-25 已改并复跑通过）。
    """
    truncated = (
        "【读者画像】paper_id：arXiv:2610.28912；推断方向：Prolog 教学\n"
        "【定级表】\n"
        "| 判据 | 分 | 证据（≤20字） | 事实/推测 |\n"
        "|"
    )
    assert parse_grade(truncated) == (None, None)


async def test_off_mode_returns_zero_calls() -> None:
    settings = Settings(llm=LLMSettings(api_key="", base_url=""))
    report = await run_grade(settings=settings, today=DAY)
    assert report.mode == "off"
    assert report.llm_calls == 0


async def test_candidate_trigger_and_persist(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(
        db,
        [
            make_paper("arXiv:mature", published="2025-01-01T00:00:00Z"),
            make_paper("arXiv:cited", published="2026-10-20T00:00:00Z", citations=5),
            make_paper("arXiv:fresh", published="2026-10-20T00:00:00Z", citations=0),
        ],
    )
    fake = FakeGradeLLM(tier=1, confidence="高")
    report = await run_grade(settings=fast_settings(), db_path=db, client=fake, today=DAY)

    assert report.requested == 2  # mature + cited；fresh 不触发
    assert report.graded == 2
    assert report.cost_cny > 0

    papers = load_papers(db)
    assert papers["arXiv:mature"].tier == 1
    assert papers["arXiv:mature"].grade_confidence == "高"
    assert papers["arXiv:mature"].graded_at == "2026-10-24"
    assert papers["arXiv:mature"].grade_content_hash == papers["arXiv:mature"].content_hash
    assert papers["arXiv:fresh"].tier is None

    engine = engine_for(db)
    with Session(engine) as session:
        usages = list(session.exec(select(LLMUsage)).all())
    assert len(usages) == 2
    assert all(u.purpose == "grading" for u in usages)


async def test_skip_already_graded(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A")])
    settings = fast_settings()
    await run_grade(settings=settings, db_path=db, client=FakeGradeLLM(), today=DAY)

    fake2 = FakeGradeLLM()
    report = await run_grade(settings=settings, db_path=db, client=fake2, today=DAY)
    assert report.requested == 0
    assert report.skipped == 1
    assert fake2.calls == 0


async def test_force_regrades(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A")])
    settings = fast_settings()
    await run_grade(settings=settings, db_path=db, client=FakeGradeLLM(tier=1), today=DAY)

    fake2 = FakeGradeLLM(tier=2)
    report = await run_grade(settings=settings, db_path=db, client=fake2, today=DAY, force=True)
    assert report.graded == 1
    assert fake2.calls == 1
    assert load_papers(db)["arXiv:A"].tier == 2


async def test_parse_failure_degrades_without_hash(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A")])
    fake = FakeGradeLLM()
    fake.complete = _no_tier_complete  # type: ignore[method-assign]
    report = await run_grade(settings=fast_settings(), db_path=db, client=fake, today=DAY)
    assert report.graded == 0
    assert report.llm_calls == 1  # ★ 真实调用了就计数
    assert report.cost_cny > 0
    assert "llm" in report.degraded
    paper = load_papers(db)["arXiv:A"]
    assert paper.tier is None
    assert paper.grade_content_hash is None  # 下次重试
    engine = engine_for(db)
    with Session(engine) as session:
        usage = list(session.exec(select(LLMUsage)).all())
    assert len(usage) == 1 and usage[0].purpose == "grading"


async def _no_tier_complete(messages, *, json_schema=None, json_mode=True):
    return LLMResponse(content="输出没有 Tier 字样", tokens_in=1, tokens_out=1)


async def test_budget_gate_blocks(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A")])
    fake = FakeGradeLLM()
    report = await run_grade(
        settings=fast_settings(daily_limit_cny=0.0), db_path=db, client=fake, today=DAY
    )
    assert report.blocked == 1
    assert report.llm_calls == 0
    assert "budget" in report.degraded


async def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A")])
    report = await run_grade(
        settings=fast_settings(), db_path=db, client=FakeGradeLLM(), today=DAY, dry_run=True
    )
    assert report.graded == 1
    assert load_papers(db)["arXiv:A"].tier is None


async def test_report_new_graded_filled(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A", title="Graded Paper")])
    await run_grade(settings=fast_settings(), db_path=db, client=FakeGradeLLM(), today=DAY)
    engine = engine_for(db)
    with Session(engine) as session:
        report = build_daily_report(session, today=DAY, watchlist=WatchlistConfig())
    assert any(hit.id == "arXiv:A" for hit in report.new_graded)
