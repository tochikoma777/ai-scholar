"""L3a-2：enhance 管道测试。

★ 用注入的 FakeLLM 做管道级断言；off 档断言零调用。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from sqlmodel import Session, SQLModel, select

from ai_scholar.budget import BudgetSettings
from ai_scholar.config import LLMSettings, Settings
from ai_scholar.llm import LLMResponse
from ai_scholar.models import WatchlistConfig, WatchlistGroup
from ai_scholar.pipeline.enhance import run_enhance
from ai_scholar.store.db import engine_for
from ai_scholar.store.models import LINK_TYPE_SIMILARITY, LLMUsage, Paper, PaperLink

WATCHLIST = WatchlistConfig(
    default_threshold=0.9,
    groups=[WatchlistGroup(name="agents", weight=1.0, keywords=["retrieval"])],
)


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


def make_paper(pid: str, title: str, *, abstract: str = "retrieval augmented generation") -> Paper:
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
        url=f"https://arxiv.org/abs/{pid}",
        arxiv_id=None,
        doi=None,
        primary_category="cs.AI",
        categories_json="[]",
        content_hash=f"h-{pid}",
    )


def seed(db_path: Path, papers: list[Paper], links: list[PaperLink] | None = None) -> None:
    engine = engine_for(db_path)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for paper in papers:
            session.add(paper)
        for link in links or []:
            session.add(link)
        session.commit()


class FakeLLM:
    """按 prompt 里的 paper_id 回显正确 JSON（增强 + 关联解读都支持）。"""

    def __init__(
        self, *, one_line: str = "一句话", why: str = "为什么重要", tags: list[str] | None = None
    ):
        self.one_line = one_line
        self.why = why
        self.tags = tags or ["agent"]
        self.calls = 0
        self.last_prompt = ""

    async def complete(
        self, messages: list[dict[str, str]], *, json_schema: dict | None = None
    ) -> LLMResponse:
        self.calls += 1
        text = messages[0]["content"]
        self.last_prompt = text
        if "论文 A ID" in text:
            a = re.search(r"论文 A ID：`([^`]+)`", text)
            b = re.search(r"论文 B ID：`([^`]+)`", text)
            content = json.dumps(
                {"paper_a_id": a.group(1), "paper_b_id": b.group(1), "interpretation": "同一方向"}
            )
        else:
            m = re.search(r"论文 ID：`([^`]+)`", text)
            content = json.dumps(
                {
                    "paper_id": m.group(1),
                    "one_line": self.one_line,
                    "why": self.why,
                    "method_tags": self.tags,
                }
            )
        return LLMResponse(content=content, tokens_in=10, tokens_out=5, cache_hit=False)


def load_papers(db_path: Path) -> list[Paper]:
    engine = engine_for(db_path)
    with Session(engine) as session:
        return list(session.exec(select(Paper)).all())


async def test_off_mode_returns_zero_calls(tmp_path: Path) -> None:
    settings = Settings(llm=LLMSettings(api_key="", base_url=""))
    report = await run_enhance(settings=settings)
    assert report.mode == "off"
    assert report.llm_calls == 0


async def test_enhance_persists_and_records_usage(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A", "RAG Survey"), make_paper("arXiv:B", "Retrieval Agents")])
    fake = FakeLLM(why="它把检索接入智能体主循环")
    report = await run_enhance(
        settings=fast_settings(), db_path=db, client=fake, watchlist=WATCHLIST
    )

    assert report.enhanced == 2
    assert report.llm_calls == 2
    assert report.cost_cny < 0.001  # ★ 按实际 token 记账，不是 max_tokens 保守估算
    assert "标题：Retrieval Agents" in fake.last_prompt
    assert "摘要：retrieval augmented generation" in fake.last_prompt

    papers = {p.id: p for p in load_papers(db)}
    assert papers["arXiv:A"].one_line == "一句话"
    assert papers["arXiv:A"].why == "它把检索接入智能体主循环"
    assert papers["arXiv:A"].enhanced_at is not None
    assert papers["arXiv:A"].enhance_content_hash == papers["arXiv:A"].content_hash

    engine = engine_for(db)
    with Session(engine) as session:
        usages = list(session.exec(select(LLMUsage)).all())
    assert len(usages) == 2
    assert all(u.purpose == "enhance" for u in usages)
    assert all(u.cost_cny is not None and u.cost_cny > 0 for u in usages)


async def test_skip_already_enhanced(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    paper = make_paper("arXiv:A", "RAG Survey")
    seed(db, [paper])
    fake = FakeLLM()
    settings = fast_settings()
    await run_enhance(settings=settings, db_path=db, client=fake, watchlist=WATCHLIST)
    assert fake.calls == 1

    fake2 = FakeLLM()
    report = await run_enhance(settings=settings, db_path=db, client=fake2, watchlist=WATCHLIST)
    assert report.skipped == 1
    assert report.requested == 0
    assert fake2.calls == 0


async def test_force_reenhances(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A", "RAG Survey")])
    settings = fast_settings()
    await run_enhance(settings=settings, db_path=db, client=FakeLLM(), watchlist=WATCHLIST)

    fake2 = FakeLLM(one_line="新一句话")
    report = await run_enhance(
        settings=settings, db_path=db, client=fake2, watchlist=WATCHLIST, force=True
    )
    assert report.enhanced == 1
    assert fake2.calls == 1
    papers = {p.id: p for p in load_papers(db)}
    assert papers["arXiv:A"].one_line == "新一句话"


async def test_budget_gate_blocks(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A", "RAG Survey"), make_paper("arXiv:B", "Retrieval Agents")])
    fake = FakeLLM()
    report = await run_enhance(
        settings=fast_settings(daily_limit_cny=0.0), db_path=db, client=fake, watchlist=WATCHLIST
    )
    assert report.blocked == 2
    assert report.llm_calls == 0
    assert "budget" in report.degraded


async def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A", "RAG Survey")])
    fake = FakeLLM()
    report = await run_enhance(
        settings=fast_settings(), db_path=db, client=fake, watchlist=WATCHLIST, dry_run=True
    )
    assert report.enhanced == 1
    assert report.llm_calls == 1
    papers = {p.id: p for p in load_papers(db)}
    assert papers["arXiv:A"].enhance_content_hash is None
    engine = engine_for(db)
    with Session(engine) as session:
        assert len(list(session.exec(select(LLMUsage)).all())) == 0


async def test_paper_id_mismatch_skips(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A", "RAG Survey")])
    fake = FakeLLM()
    # 篡改回显 id，模拟幻觉错位
    orig = fake.complete

    async def wrong(messages, *, json_schema=None):
        resp = await orig(messages, json_schema=json_schema)
        resp.content = json.dumps(
            {"paper_id": "arXiv:OTHER", "one_line": "x", "why": "y", "method_tags": []}
        )
        return resp

    fake.complete = wrong  # type: ignore[method-assign]
    report = await run_enhance(
        settings=fast_settings(), db_path=db, client=fake, watchlist=WATCHLIST
    )
    assert report.enhanced == 0
    assert "llm" in report.degraded


async def test_link_interpretation_writes_edge(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    a = make_paper("arXiv:A", "Paper A", abstract="unrelated topic alpha")
    b = make_paper("arXiv:B", "Paper B", abstract="unrelated topic beta")
    link = PaperLink(
        src_id="arXiv:A",
        dst_id="arXiv:B",
        link_type=LINK_TYPE_SIMILARITY,
        weight=0.5,
        evidence="相似 0.50",
    )
    seed(db, [a, b], links=[link])
    fake = FakeLLM()
    report = await run_enhance(
        settings=fast_settings(), db_path=db, client=fake, watchlist=WATCHLIST
    )
    assert report.links_interpreted == 1
    engine = engine_for(db)
    with Session(engine) as session:
        row = session.get(PaperLink, ("arXiv:A", "arXiv:B", LINK_TYPE_SIMILARITY))
    assert row is not None and row.interpretation == "同一方向"
