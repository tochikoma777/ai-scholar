"""文献定级（L3b-1，fast）：三判据 → Tier 1/2/3。

★ 延迟定级（§5.5.2）：只在论文成熟（published_at ≥ 180 天）或引用数达标时定级，
  新论文不立刻定级（结构权重判据需要施引数据）。

★ 幂等：`grade_content_hash == content_hash` 且 tier 非空 ⇒ 跳过；`--force` 重做。

★ `grading.j2` 输出是**文本表**，这里解析 `Tier N` + 置信度；解析失败只 degraded，
  不写幂等指纹（下次重试），不中断流水线。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from ai_scholar.budget import check_budget, estimate_cost
from ai_scholar.config import RunMode, Settings, load_settings
from ai_scholar.llm import LLMClient, NullLLM, build_client
from ai_scholar.pipeline.llm_support import (
    actual_cost,
    authors_text,
    estimate_input_tokens,
    render_prompt,
    spent_in_windows,
    year_text,
)
from ai_scholar.store.db import default_db_path
from ai_scholar.store.models import (
    SIGNAL_REJECTED,
    SIGNAL_SAMPLE,
    LLMUsage,
    Paper,
    PaperReference,
)
from ai_scholar.utils.errors import LLMError
from ai_scholar.utils.timeutil import age_days_since, today_local

_TIER_RE = re.compile(r"Tier\s*([123])", re.IGNORECASE)
_CONF_RE = re.compile(r"【置信度】\s*(高|中|低)")
_REFERENCES_LIMIT = 30


@dataclass
class GradeReport:
    """一次 grade 的汇总（CLI 据此打印，退出码不受降级影响）。"""

    run_id: str = ""
    mode: str = "fast"
    requested: int = 0
    graded: int = 0
    blocked: int = 0
    skipped: int = 0
    llm_calls: int = 0
    cost_cny: float = 0.0
    degraded: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False


def parse_grade(text: str) -> tuple[int | None, str | None]:
    """从 grading.j2 文本表解析 `(tier, confidence)`；解析失败返回 `(None, None)`。"""
    tier_match = _TIER_RE.search(text)
    conf_match = _CONF_RE.search(text)
    tier = int(tier_match.group(1)) if tier_match else None
    confidence = conf_match.group(1) if conf_match else None
    return tier, confidence


def _references_text(session: Session, paper_id: str) -> str:
    rows = session.exec(select(PaperReference).where(PaperReference.paper_id == paper_id)).all()
    refs = [row.ref_external_id for row in rows][:_REFERENCES_LIMIT]
    return ", ".join(refs) if refs else "（暂无）"


def _trigger_met(paper: Paper, *, day: date, settings: Settings) -> bool:
    """延迟定级触发：成熟（published_at ≥ 180 天）或引用数达标。"""
    if paper.signal_state in (SIGNAL_REJECTED, SIGNAL_SAMPLE):
        return False
    age = age_days_since(paper.published_at, day)
    mature = age is not None and age >= settings.signals.maturity_days
    cited = (paper.citation_count or 0) >= settings.signals.min_citations
    return mature or cited


def _needs_grade(paper: Paper, *, force: bool) -> bool:
    """是否需要（重新）定级。幂等：已定级且内容未变 ⇒ False。"""
    if force:
        return True
    if paper.tier is None:
        return True
    return paper.grade_content_hash != paper.content_hash


def _select_candidates(
    session: Session, *, day: date, settings: Settings, limit: int, force: bool
) -> tuple[list[Paper], int]:
    papers = list(session.exec(select(Paper)).all())
    eligible = [p for p in papers if _trigger_met(p, day=day, settings=settings)]
    candidates = [p for p in eligible if _needs_grade(p, force=force)]
    skipped = len(eligible) - len(candidates)
    candidates.sort(key=lambda p: ((p.citation_count or 0), p.first_seen_at or ""), reverse=True)
    return candidates[:limit], skipped


async def run_grade(
    *,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    today: date | None = None,
    settings: Settings | None = None,
    db_path: Path | None = None,
    client: LLMClient | NullLLM | None = None,
) -> GradeReport:
    """跑一次定级。★ 单条失败只记 degraded，不影响退出码；off 档直接返回。"""
    settings = settings or load_settings()
    report = GradeReport(dry_run=dry_run)
    report.run_id = uuid.uuid4().hex[:12]
    day = today or today_local()

    if settings.effective_mode() == RunMode.OFF:
        report.mode = "off"
        return report

    db_file = Path(db_path) if db_path else default_db_path(settings)
    engine = create_engine(f"sqlite:///{db_file}", echo=False)
    SQLModel.metadata.create_all(engine)

    effective_limit = limit if limit is not None else settings.grade.default_limit
    provider = settings.llm.provider
    model = settings.llm.fast_model
    max_tokens = settings.llm.max_tokens_fast

    owns_client = client is None
    llm = client or build_client(settings)
    calls = 0

    try:
        with Session(engine) as session:
            candidates, skipped = _select_candidates(
                session, day=day, settings=settings, limit=effective_limit, force=force
            )
            report.requested = len(candidates)
            report.skipped = skipped

            for i, paper in enumerate(candidates):
                prompt = render_prompt(
                    "grading.j2",
                    paper_id=paper.id,
                    title=paper.title,
                    authors_and_affiliations=authors_text(paper),
                    year=year_text(paper),
                    venue=paper.venue or "—",
                    abstract=paper.abstract or "",
                    references=_references_text(session, paper.id),
                    citation_count=paper.citation_count or 0,
                    citing_titles=None,
                    lens_answer_5=None,
                )
                estimate = estimate_cost(
                    settings.budget,
                    provider,
                    model,
                    estimate_input_tokens(prompt),
                    max_tokens,
                )
                state = spent_in_windows(session, today=day, calls_this_run=calls)
                decision = check_budget(settings.budget, state, estimate)
                if not decision.allowed:
                    report.blocked = len(candidates) - i
                    report.degraded["budget"] = decision.reason
                    break

                calls += 1
                if dry_run:
                    report.graded += 1
                    report.llm_calls += 1
                    report.cost_cny += estimate.cost_cny
                    continue

                try:
                    response = await llm.complete(
                        [{"role": "user", "content": prompt}], json_mode=False
                    )
                except LLMError as exc:
                    report.degraded["llm"] = f"{type(exc).__name__}: {exc}"
                    continue

                # ★ 只要真实调用了就记账（即使解析失败，成本也已发生）
                grade_cost = actual_cost(settings, provider, model, response)
                session.add(
                    LLMUsage(
                        run_id=report.run_id,
                        model=model,
                        tokens_in=response.tokens_in,
                        tokens_out=response.tokens_out,
                        cost_cny=grade_cost,
                        cache_hit=response.cache_hit,
                        purpose="grading",
                    )
                )
                report.llm_calls += 1
                report.cost_cny += grade_cost

                tier, confidence = parse_grade(response.content)
                paper.grade_text = response.content
                paper.grade_confidence = confidence
                if tier is None:
                    report.degraded["llm"] = "无法从输出解析 Tier（下次重试）"
                    continue
                if paper.id not in response.content:
                    report.degraded["llm"] = "paper_id 未回显（结果丢弃）"
                    continue

                paper.tier = tier
                paper.graded_at = day.isoformat()
                paper.grade_content_hash = paper.content_hash
                report.graded += 1

            if not dry_run:
                session.commit()
    finally:
        if owns_client:
            await llm.aclose()

    return report
