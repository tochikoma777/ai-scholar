"""LLM 增强（L3a）：一句话 / 为什么重要 / 方法标签 + 关联解读。

★ 设计（§3.6 / §8.4a）：
- 增强对象：watchlist 命中的**未增强**论文（`enhance_content_hash != content_hash`），
  按命中分降序 Top N（默认 20，可配）。
- 三档模式：off 档**直接返回**（0 调用、退出 0）；fast 走 `LLMClient`。
- 预算闸门：每次调用前 `estimate_cost` + `check_budget`，拦截即停止（退出码 0）。
- 记账：调用后写 `llm_usage`（tokens / model / cost_cny / cache_hit）。
- 持久化：`papers.one_line/why/method_tags_json` + `enhanced_at` + `enhance_content_hash`；
  关联解读写 `paper_links.interpretation`。同条目不重复生成。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlmodel import Session, SQLModel, col, create_engine, select

from ai_scholar.budget import check_budget, estimate_cost
from ai_scholar.config import RunMode, Settings, load_settings, load_watchlist, match_watchlist
from ai_scholar.llm import LLMClient, NullLLM, build_client, parse_json_content
from ai_scholar.models import WatchlistConfig
from ai_scholar.pipeline.llm_support import (
    actual_cost,
    authors_text,
    estimate_input_tokens,
    render_prompt,
    spent_in_windows,
    year_text,
)
from ai_scholar.store.db import default_db_path
from ai_scholar.store.models import LINK_TYPE_SIMILARITY, LLMUsage, Paper, PaperLink
from ai_scholar.utils.errors import LLMError
from ai_scholar.utils.timeutil import today_local

ENHANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "paper_id": {"type": "string"},
        "one_line": {"type": "string"},
        "why": {"type": "string"},
        "method_tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["paper_id", "one_line", "why", "method_tags"],
}

LINK_SCHEMA = {
    "type": "object",
    "properties": {
        "paper_a_id": {"type": "string"},
        "paper_b_id": {"type": "string"},
        "interpretation": {"type": "string"},
    },
    "required": ["paper_a_id", "paper_b_id", "interpretation"],
}


@dataclass
class EnhanceReport:
    """一次 enhance 的汇总（CLI 据此打印，退出码不受降级影响）。"""

    run_id: str = ""
    mode: str = "fast"
    requested: int = 0  # 本次选中要增强的论文数
    enhanced: int = 0  # 实际增强成功的论文数
    blocked: int = 0  # 因预算闸门未执行的论文数
    skipped: int = 0  # 已增强（enhance_content_hash 一致）而跳过的命中论文数
    llm_calls: int = 0
    cost_cny: float = 0.0
    links_interpreted: int = 0
    degraded: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False


def _select_candidates(
    session: Session,
    *,
    today: date,
    limit: int,
    report: EnhanceReport,
    watchlist: WatchlistConfig | None = None,
    force: bool = False,
) -> list[tuple[Paper, float]]:
    """watchlist 命中的未增强论文，按 (命中分, first_seen) 降序。"""
    watchlist = watchlist or load_watchlist()
    candidates: list[tuple[Paper, float]] = []
    for paper in session.exec(select(Paper)).all():
        hit = match_watchlist(watchlist, paper.title, paper.abstract or "")
        if not hit.matched:
            continue
        if (
            not force
            and paper.enhance_content_hash is not None
            and paper.enhance_content_hash == paper.content_hash
        ):
            report.skipped += 1
            continue
        candidates.append((paper, hit.score))
    candidates.sort(key=lambda t: (t[1], t[0].first_seen_at or ""), reverse=True)
    return candidates[:limit]


def _select_links(
    session: Session, *, strong_weight: float, limit: int, force: bool
) -> list[PaperLink]:
    """强相似边（weight ≥ 阈值）中未解读的 Top M。"""
    rows = list(
        session.exec(
            select(PaperLink).where(
                PaperLink.link_type == LINK_TYPE_SIMILARITY,
                col(PaperLink.weight) >= strong_weight,
            )
        ).all()
    )
    if not force:
        rows = [r for r in rows if not r.interpretation]
    rows.sort(key=lambda r: r.weight or 0.0, reverse=True)
    return rows[:limit]


async def run_enhance(
    *,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    today: date | None = None,
    settings: Settings | None = None,
    db_path: Path | None = None,
    client: LLMClient | NullLLM | None = None,
    watchlist: WatchlistConfig | None = None,
) -> EnhanceReport:
    """跑一次增强。★ 单条失败只记 degraded，不影响退出码；off 档直接返回。"""
    settings = settings or load_settings()
    report = EnhanceReport(dry_run=dry_run)
    report.run_id = uuid.uuid4().hex[:12]
    day = today or today_local()

    if settings.effective_mode() == RunMode.OFF:
        report.mode = "off"
        return report

    db_file = Path(db_path) if db_path else default_db_path(settings)
    engine = create_engine(f"sqlite:///{db_file}", echo=False)
    SQLModel.metadata.create_all(engine)

    effective_limit = limit if limit is not None else settings.enhance.default_limit
    link_pairs_limit = settings.enhance.link_pairs_limit
    strong_weight = settings.relate.similarity_strong_weight
    provider = settings.llm.provider
    model = settings.llm.fast_model
    max_tokens = settings.llm.max_tokens_fast

    owns_client = client is None
    llm = client or build_client(settings)
    calls = 0

    try:
        with Session(engine) as session:
            candidates = _select_candidates(
                session,
                today=day,
                limit=effective_limit,
                report=report,
                watchlist=watchlist,
                force=force,
            )
            report.requested = len(candidates)
            by_id = {p.id: p for p in session.exec(select(Paper)).all()}

            for i, (paper, _score) in enumerate(candidates):
                prompt = render_prompt(
                    "enhance.j2",
                    paper_id=paper.id,
                    title=paper.title,
                    abstract=paper.abstract or "",
                    authors_and_affiliations=authors_text(paper),
                    year=year_text(paper),
                    venue=paper.venue or "—",
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
                    report.enhanced += 1
                    report.llm_calls += 1
                    report.cost_cny += estimate.cost_cny
                    continue

                try:
                    response = await llm.complete(
                        [{"role": "user", "content": prompt}], json_schema=ENHANCE_SCHEMA
                    )
                except LLMError as exc:
                    report.degraded[f"llm:{paper.id}"] = f"{type(exc).__name__}: {exc}"
                    continue

                # ★ 只要真实调用了就记账（**在**解析/校验之前）：解析失败的钱已经花出去了，
                #   漏记会让 `cost` 与预算闸门失明（`spent_in_windows` 读的就是这张表）。
                papers_cost = actual_cost(settings, provider, model, response)
                session.add(
                    LLMUsage(
                        run_id=report.run_id,
                        model=model,
                        tokens_in=response.tokens_in,
                        tokens_out=response.tokens_out,
                        cost_cny=papers_cost,
                        cache_hit=response.cache_hit,
                        purpose="enhance",
                    )
                )
                report.llm_calls += 1
                report.cost_cny += papers_cost

                try:
                    data = parse_json_content(response.content)
                except LLMError as exc:
                    report.degraded[f"llm:{paper.id}"] = f"{type(exc).__name__}: {exc}"
                    continue
                if data.get("paper_id") != paper.id:
                    report.degraded[f"llm:{paper.id}"] = (
                        f"paper_id 回显不一致：{data.get('paper_id')!r}"
                    )
                    continue

                paper.one_line = data.get("one_line") or None
                paper.why = data.get("why") or None
                paper.method_tags_json = json.dumps(
                    data.get("method_tags") or [], ensure_ascii=False
                )
                paper.enhanced_at = day.isoformat()
                paper.enhance_content_hash = paper.content_hash
                report.enhanced += 1

            # 关联解读（强相似边）
            for edge in _select_links(
                session, strong_weight=strong_weight, limit=link_pairs_limit, force=force
            ):
                paper_a = by_id.get(edge.src_id)
                paper_b = by_id.get(edge.dst_id)
                if paper_a is None or paper_b is None:
                    continue
                prompt = render_prompt(
                    "link_interpret.j2",
                    paper_a_id=paper_a.id,
                    paper_a_title=paper_a.title,
                    paper_a_abstract=paper_a.abstract or "",
                    paper_b_id=paper_b.id,
                    paper_b_title=paper_b.title,
                    paper_b_abstract=paper_b.abstract or "",
                    similarity=f"{edge.weight:.2f}",
                )
                estimate = estimate_cost(
                    settings.budget,
                    provider,
                    model,
                    estimate_input_tokens(prompt),
                    max_tokens,
                )
                state = spent_in_windows(session, today=day, calls_this_run=calls)
                if not check_budget(settings.budget, state, estimate).allowed:
                    report.degraded["budget"] = "预算闸门拦截（关联解读）"
                    break

                calls += 1
                if dry_run:
                    report.links_interpreted += 1
                    report.llm_calls += 1
                    report.cost_cny += estimate.cost_cny
                    continue

                try:
                    response = await llm.complete(
                        [{"role": "user", "content": prompt}], json_schema=LINK_SCHEMA
                    )
                except LLMError as exc:
                    report.degraded["link"] = f"{type(exc).__name__}: {exc}"
                    continue

                # ★ 同上：真实调用即记账，放在解析之前
                link_cost = actual_cost(settings, provider, model, response)
                session.add(
                    LLMUsage(
                        run_id=report.run_id,
                        model=model,
                        tokens_in=response.tokens_in,
                        tokens_out=response.tokens_out,
                        cost_cny=link_cost,
                        cache_hit=response.cache_hit,
                        purpose="enhance_link",
                    )
                )
                report.llm_calls += 1
                report.cost_cny += link_cost

                try:
                    data = parse_json_content(response.content)
                except LLMError as exc:
                    report.degraded["link"] = f"{type(exc).__name__}: {exc}"
                    continue
                edge.interpretation = data.get("interpretation") or None
                report.links_interpreted += 1

            if not dry_run:
                session.commit()
    finally:
        if owns_client:
            await llm.aclose()

    return report
