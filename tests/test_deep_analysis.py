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
from ai_scholar.pipeline.deep_analysis import analysis_path, run_deep_analysis
from ai_scholar.pipeline.report import build_daily_report
from ai_scholar.store.db import engine_for
from ai_scholar.store.models import LLMUsage, Paper

DAY = date(2026, 10, 24)
WATCHLIST = WatchlistConfig(
    default_threshold=0.9,
    groups=[WatchlistGroup(name="agents", weight=1.0, keywords=["retrieval"])],
)


def strong_settings(data_dir: str | None = None, **budget_overrides: object) -> Settings:
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
    settings = Settings(
        llm=LLMSettings(api_key="k", base_url="https://api.deepseek.com"), budget=budget
    )
    # ★ 测试必须把 data_dir 指到 tmp_path：否则深度分析产物会写进**仓库**的 data/analyses/
    if data_dir is not None:
        settings.app.data_dir = data_dir
    return settings


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


# ★ 形态照抄真实产物：真实模型**经常不回显 paper_id**（Actions 2026-10-26 实测 4/5），
#   所以假模型默认也**不回显**，只给结构 + 足够长度（校验看结构，不看回显）。
FAKE_BODY = (
    "## TL;DR（三句半）\n坐标：演进——把 A 接到 B。\n痛点：现有做法贵。\n"
    "手术：换掉中间层。\n疗效：指标 +3.2。\n代价：多花 20% 显存。\n\n"
    "## 六段\n### 总体介绍\n"
    + "这是一段用来满足最小长度校验的正文。"
    * 24
    + "\n### 核心思想\n同上。\n### 技术路线与结构特点\n同上。\n"
    "### 主要贡献\n同上。\n### 实验与证据\n同上。\n### 历史地位\n待观察。\n"
)


class FakeDeepLLM:
    def __init__(self, *, echo_id: bool = False, body: str | None = None, tokens_out: int = 500):
        self.echo_id = echo_id
        self.body = body
        self.tokens_out = tokens_out
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
        body = self.body if self.body is not None else FAKE_BODY
        if self.echo_id:
            body = f"paper_id: {pid}\n{body}"
        return LLMResponse(
            content=body, tokens_in=1000, tokens_out=self.tokens_out, cache_hit=False
        )


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
        settings=strong_settings(str(tmp_path)),
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
    """配额口径 = **本周已产出的篇数**（不是调用次数）。"""
    db = tmp_path / "d.db"
    done = [make_paper(f"arXiv:D{i}") for i in range(5)]
    for paper in done:  # 本周已分析出的 5 篇
        paper.deep_analysis_md = "## 六段\n已有"
        paper.deep_content_hash = paper.content_hash
        paper.deep_analyzed_at = DAY.isoformat()
        paper.deep_status = "done"
    seed(db, [*done, make_paper("arXiv:A")])
    fake = FakeDeepLLM()
    report = await run_deep_analysis(
        settings=strong_settings(str(tmp_path)),
        db_path=db,
        client=fake,
        watchlist=WATCHLIST,
        today=DAY,
    )
    assert report.blocked == 1
    assert report.llm_calls == 0
    assert "quota" in report.degraded


async def test_failed_calls_do_not_burn_weekly_quota(tmp_path: Path) -> None:
    """★ 回归（2026-10-26 Actions 实锤）：4 次废调用烧掉整周额度 ⇒ 一周不能分析。

    配额 = 注意力预算（篇），失败调用只该花当天的钱（有 daily budget 兜底），
    不该把**本周**的额度一起吃掉。
    """
    db = tmp_path / "d.db"
    failed_usage = [
        LLMUsage(
            run_id=f"r{i}",
            model="deepseek-v4-pro",
            tokens_in=1,
            tokens_out=1,
            cost_cny=0.001,
            purpose="deep_analysis",
            created_at=f"{DAY.isoformat()}T12:00:00+00:00",
        )
        for i in range(5)
    ]
    seed(db, [make_paper("arXiv:A")], usage=failed_usage)
    report = await run_deep_analysis(
        settings=strong_settings(str(tmp_path)),
        db_path=db,
        client=FakeDeepLLM(),
        watchlist=WATCHLIST,
        today=DAY,
    )
    assert report.blocked == 0, "废调用不该占满周配额"
    assert report.analyzed == 1


async def test_idempotent_and_force(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A")])
    settings = strong_settings(str(tmp_path))
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
    """★ 结构不达标的输出：判废、不写指纹（下次重试）、但**成本照样记账**。"""
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A")])
    report = await run_deep_analysis(
        settings=strong_settings(str(tmp_path)),
        db_path=db,
        client=FakeDeepLLM(body="## TL;DR\n被截断了"),
        watchlist=WATCHLIST,
        today=DAY,
    )
    assert report.analyzed == 0
    assert report.llm_calls == 1  # 真实调用即记账
    assert any(key.startswith("llm:") for key in report.degraded), report.degraded
    assert "输出过短" in next(v for k, v in report.degraded.items() if k.startswith("llm:"))
    paper = load_papers(db)["arXiv:A"]
    assert paper.deep_status == "failed"
    assert paper.deep_content_hash is None


def test_validate_output_accepts_no_id_echo() -> None:
    """★ 回归：真实模型常不回显 id —— 有结构 + 足够长度就该通过。"""
    from ai_scholar.pipeline.deep_analysis import validate_output

    ok, reason = validate_output(FAKE_BODY)
    assert ok, reason
    assert not validate_output("")[0]
    assert not validate_output("## TL;DR\n短")[0]
    assert not validate_output("很长但没结构的正文" * 50)[0]


async def test_check_runs_even_in_off_mode(tmp_path: Path) -> None:
    """★ 回归（2026-10-26 Actions 假绿）：`--check` 不能因 off 档直接返回。

    daily.yml 的 check 步骤没有注入 LLM secret ⇒ effective_mode=OFF ⇒
    旧实现直接 return，于是"零候选、OK 齐备"——**假绿**。
    """
    db = tmp_path / "d.db"
    paper = make_paper("arXiv:A")
    paper.deep_analysis_md = "## 六段\n存在分析但没有真源"
    paper.deep_content_hash = paper.content_hash
    paper.deep_analyzed_at = DAY.isoformat()
    paper.deep_status = "done"
    seed(db, [paper])
    settings = strong_settings(str(tmp_path))
    settings.llm.api_key = ""  # 强制 off 档

    report = await run_deep_analysis(
        settings=settings,
        db_path=db,
        client=FakeDeepLLM(),
        watchlist=WATCHLIST,
        today=DAY,
        check=True,
    )
    assert report.check is True
    assert report.requested == 1, "off 档不该让 check 变成空跑"
    assert report.analyses_missing == ["arXiv:A"]


async def test_fulltext_is_used_and_not_persisted(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A", arxiv_id="2610.00001")])

    async def fake_fetch(http, arxiv_id, *, max_chars=60000, min_chars=2000):
        return "FULLTEXT BODY"

    monkeypatch.setattr("ai_scholar.pipeline.deep_analysis.fetch_arxiv_html", fake_fetch)
    fake = FakeDeepLLM()
    report = await run_deep_analysis(
        settings=strong_settings(str(tmp_path)),
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
        settings=strong_settings(str(tmp_path)),
        db_path=db,
        client=FakeDeepLLM(),
        watchlist=WATCHLIST,
        today=DAY,
    )
    engine = engine_for(db)
    with Session(engine) as session:
        report = build_daily_report(session, today=DAY, watchlist=WATCHLIST)
    assert any(hit.id == "arXiv:A" for hit in report.deep_analyzed)


# ── L7-2：D 方案排序（热度 / 经典交替）───────────────────────────────────
def test_alternate_heat_and_classic() -> None:
    """奇数轮热度侧、偶数轮经典侧（交替）。"""
    from ai_scholar.pipeline.deep_analysis import _alternate

    heat = [make_paper(f"H{i}") for i in range(3)]
    classic = [make_paper(f"C{i}") for i in range(3)]
    picked = _alternate(heat, classic, limit=4)
    assert [p.id for p in picked] == ["H0", "C0", "H1", "C1"]


def test_alternate_degrades_to_one_side() -> None:
    """★ 经典池为空 ⇒ 自动退化为热度侧（设计原文的预期行为）。"""
    from ai_scholar.pipeline.deep_analysis import _alternate

    heat = [make_paper("H0"), make_paper("H1")]
    assert [p.id for p in _alternate(heat, [], limit=5)] == ["H0", "H1"]
    classic = [make_paper("C0")]
    assert [p.id for p in _alternate([], classic, limit=2)] == ["C0"]


def test_heat_side_requires_hf_score() -> None:
    """热度侧 = hf_score 非空；★ 缺失**不当作 0**（直接不入侧）。"""
    from ai_scholar.pipeline.deep_analysis import _heat_side

    hot = make_paper("HOT", abstract="unrelated")
    hot.hf_score = 42
    cold = make_paper("COLD", abstract="unrelated")
    cold.hf_score = None
    rows = _heat_side([hot, cold], day=DAY, lookback_days=1)
    assert [p.id for p in rows] == ["HOT"]


def test_classic_side_three_sources() -> None:
    """经典侧三来源：成熟（≥180 天）/ 被引≥3 / 种子清单。"""
    from ai_scholar.pipeline.deep_analysis import _classic_side

    mature = make_paper("MATURE", abstract="unrelated")
    mature.published_at = "2020-01-01T00:00:00Z"
    cited = make_paper("CITED", abstract="unrelated")
    cited.citation_count = 10
    seeded = make_paper("SEED", arxiv_id="1706.03762", abstract="unrelated")
    fresh = make_paper("FRESH", abstract="unrelated")
    fresh.citation_count = 0

    rows = _classic_side(
        [mature, cited, seeded, fresh],
        day=DAY,
        maturity_days=180,
        min_citations=3,
        classic_ids={"1706.03762"},
        leaders=set(),
    )
    assert {p.id for p in rows} == {"MATURE", "CITED", "SEED"}


async def test_top_n_alternates_when_watchlist_short(tmp_path: Path) -> None:
    """watchlist 只命中 1 篇时，「当日 Top N」用 D 交替补足（热度 → 经典）。"""
    db = tmp_path / "d.db"
    watched = make_paper("arXiv:W", title="retrieval paper")
    hot = make_paper("arXiv:HOT")
    hot.hf_score = 99
    classic = make_paper("arXiv:OLD")
    classic.published_at = "2019-01-01T00:00:00Z"
    classic.citation_count = 50
    seed(db, [watched, hot, classic])

    fake = FakeDeepLLM()
    report = await run_deep_analysis(
        settings=strong_settings(str(tmp_path)),
        db_path=db,
        client=fake,
        watchlist=WATCHLIST,
        today=DAY,
        limit=3,
    )
    order = [p.id for p in load_papers(db).values() if p.deep_status == "done"]
    assert order == ["arXiv:W", "arXiv:HOT", "arXiv:OLD"], f"实际顺序 {order}"
    assert report.analyzed == 3


async def test_selected_papers_do_not_consume_quota(tmp_path: Path) -> None:
    """★ 已收藏的论文不占自动配额（`selected=1` ⇒ 用户已点名）。"""
    db = tmp_path / "d.db"
    hot = make_paper("arXiv:HOT")
    hot.hf_score = 99
    marked = make_paper("arXiv:MARKED")
    marked.hf_score = 98
    marked.selected = True  # 用户已收藏
    seed(db, [hot, marked])

    fake = FakeDeepLLM()
    report = await run_deep_analysis(
        settings=strong_settings(str(tmp_path)),
        db_path=db,
        client=fake,
        watchlist=WATCHLIST,
        today=DAY,
        limit=2,
    )
    assert report.requested == 1, "已收藏的不该进入自动选篇"
    assert load_papers(db)["arXiv:HOT"].deep_status == "done"
    assert load_papers(db)["arXiv:MARKED"].deep_analysis_md is None


# ── L7-3：深度分析完成 ⇒ selected=1 ────────────────────────────────────
async def test_analysis_sets_selected(tmp_path: Path) -> None:
    """★ 置位途径 ①：不置位 ⇒ `export`（默认只导 selected）永远导不出它。"""
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A", title="retrieval paper")])
    await run_deep_analysis(
        settings=strong_settings(str(tmp_path)),
        db_path=db,
        client=FakeDeepLLM(),
        watchlist=WATCHLIST,
        today=DAY,
    )
    assert load_papers(db)["arXiv:A"].selected is True


# ── L7-4：产物落 public 真源 data/analyses/{id}.md ──────────────────────
async def test_analysis_written_to_public_source(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    seed(db, [make_paper("arXiv:A", title="retrieval paper")])
    settings = strong_settings(str(tmp_path))
    report = await run_deep_analysis(
        settings=settings, db_path=db, client=FakeDeepLLM(), watchlist=WATCHLIST, today=DAY
    )
    assert report.analyses_written == 1

    path = analysis_path(settings, "arXiv:A")
    assert path.exists() and path.parent.name == "analyses"
    text = path.read_text(encoding="utf-8")
    assert "arXiv:A" in text and "## 六段" in text
    # ★ 幂等：重跑（force）同内容 ⇒ 不再写盘，且字节不变
    before = path.read_bytes()
    report2 = await run_deep_analysis(
        settings=settings,
        db_path=db,
        client=FakeDeepLLM(),
        watchlist=WATCHLIST,
        today=DAY,
        force=True,
    )
    assert report2.analyses_written == 0, "同内容不该重复写盘"
    assert path.read_bytes() == before


async def test_check_reports_missing_and_stale(tmp_path: Path) -> None:
    """★ `--check`：零 LLM / 零网络，缺失与过期都能报出来。"""
    db = tmp_path / "d.db"
    paper = make_paper("arXiv:A", title="retrieval paper")
    paper.deep_analysis_md = "## 六段\n内容"
    paper.deep_content_hash = paper.content_hash
    paper.deep_analyzed_at = DAY.isoformat()
    paper.deep_status = "done"
    seed(db, [paper])
    settings = strong_settings(str(tmp_path))

    # ① db 里有分析、public 真源没有 ⇒ 缺失
    missing = await run_deep_analysis(
        settings=settings,
        db_path=db,
        client=FakeDeepLLM(),
        watchlist=WATCHLIST,
        today=DAY,
        check=True,
    )
    assert missing.analyses_missing == ["arXiv:A"]
    assert missing.llm_calls == 0, "check 必须零调用"
    assert not (tmp_path / "analyses").exists(), "check 不该写任何文件"

    # ② 真的产出 ⇒ 齐备（force：让已分析过的这篇重新落一次盘）
    report = await run_deep_analysis(
        settings=settings,
        db_path=db,
        client=FakeDeepLLM(),
        watchlist=WATCHLIST,
        today=DAY,
        force=True,
    )
    # ★ ≥1：先由 sync_analyses 自愈补写，force 重分析若内容不同会再写一次
    assert report.analyses_written >= 1
    ok = await run_deep_analysis(
        settings=settings,
        db_path=db,
        client=FakeDeepLLM(),
        watchlist=WATCHLIST,
        today=DAY,
        check=True,
    )
    assert ok.analyses_missing == [], "已产出且 hash 匹配 ⇒ 齐备"

    # ③ 内容变了（content_hash 变）⇒ 产物过期
    engine = engine_for(db)
    with Session(engine) as session:
        row = session.get(Paper, "arXiv:A")
        assert row is not None
        row.content_hash = "changed"
        session.commit()
    stale = await run_deep_analysis(
        settings=settings,
        db_path=db,
        client=FakeDeepLLM(),
        watchlist=WATCHLIST,
        today=DAY,
        check=True,
    )
    assert stale.analyses_missing == ["arXiv:A"]


async def test_sync_backfills_selected_for_legacy_analyses(tmp_path: Path) -> None:
    """★ 历史遗留（L7 之前分析过但没置位）⇒ 自愈补写时一并置 `selected=1`。

    不补的话，这些论文永远导不进 vault —— DoD #3 在库内不成立。
    """
    from ai_scholar.pipeline.deep_analysis import sync_analyses

    db = tmp_path / "d.db"
    paper = make_paper("arXiv:OLD", title="legacy analyzed")
    paper.deep_analysis_md = "## 六段\n旧分析"
    paper.deep_content_hash = paper.content_hash
    paper.deep_analyzed_at = DAY.isoformat()
    paper.deep_status = "done"
    paper.selected = False  # ★ L7 之前的状态
    seed(db, [paper])
    settings = strong_settings(str(tmp_path))

    engine = engine_for(db)
    with Session(engine) as session:
        assert sync_analyses(session, settings) == 1
        session.commit()
    assert load_papers(db)["arXiv:OLD"].selected is True
    assert analysis_path(settings, "arXiv:OLD").exists()


def test_validate_output_rejects_truncation() -> None:
    """★ E6 回归：tokens_out 顶格 = 截断，结构再"像"也必须判废。"""
    from ai_scholar.pipeline.deep_analysis import validate_output

    ok, reason = validate_output(FAKE_BODY, tokens_out=8000, max_tokens=8000)
    assert not ok and "截断" in reason
    ok, _ = validate_output(FAKE_BODY, tokens_out=6500, max_tokens=8000)
    assert ok


async def test_consecutive_failures_abort_run(tmp_path: Path) -> None:
    """★ 预算保护回归：连续 2 次判废就中止，不许把剩余候选全部付费烧掉。

    背景（2026-10-26 首次 Actions）：5 次调用 1 次成功，4 次判废仍继续调用，
    白花 ¥0.2361 的 80%。失败是**有信息**的：同一模型连挂两次，后面只会更糟。
    """
    db = tmp_path / "d.db"
    seed(
        db,
        [make_paper(f"arXiv:P{i}") for i in range(3)],
    )
    fake = FakeDeepLLM(tokens_out=16000)  # 每次都"顶格" ⇒ 每次都被判废
    report = await run_deep_analysis(
        settings=strong_settings(str(tmp_path)),
        db_path=db,
        client=fake,
        watchlist=WATCHLIST,
        today=DAY,
        limit=3,
    )
    assert fake.calls == 2, f"连续失败应刹车，实际调用 {fake.calls} 次"
    assert report.blocked == 1
    assert "llm_abort" in report.degraded
    assert report.analyzed == 0
