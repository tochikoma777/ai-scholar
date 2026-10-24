"""深度分析（L3b-2，strong）：三句半 + 六段 + 背景透镜。

★ 触发（§5.5.2）：
- 通道 A（意图驱动，**不看 Tier**）：`--ids` 显式 > watchlist 命中 > 当日 Top N。
- 通道 B（价值驱动）：Tier 1 且未深度分析的补读队列。
- 注意力预算：每周最多 `deep_analysis.weekly_limit` 篇（默认 5），超限进队列不丢弃。

★ 正文：arXiv HTML **临时抓取**，抓完即弃，**永不入库**；抓不到退回仅摘要
（透镜块由 prompt 标【需正文】）。

★ 持久化：Markdown 全文 + `deep_content_hash` 幂等；失败标 `deep_status="failed"`，
不写幂等指纹（下次重试）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from ai_scholar.budget import check_budget, estimate_cost
from ai_scholar.collectors.fulltext import fetch_arxiv_html
from ai_scholar.config import RunMode, Settings, load_settings, load_watchlist, match_watchlist
from ai_scholar.llm import LLMClient, NullLLM, build_client
from ai_scholar.models import WatchlistConfig
from ai_scholar.pipeline.llm_support import (
    actual_cost,
    authors_text,
    estimate_input_tokens,
    references_text,
    render_prompt,
    spent_in_windows,
    window_starts,
    year_text,
)
from ai_scholar.store.db import default_db_path
from ai_scholar.store.models import (
    SIGNAL_REJECTED,
    SIGNAL_SAMPLE,
    LLMUsage,
    Paper,
)
from ai_scholar.utils.errors import LLMError
from ai_scholar.utils.http import HttpClient, client_from_settings
from ai_scholar.utils.timeutil import age_days_since, today_local


@dataclass
class DeepAnalysisReport:
    """一次 analyze 的汇总（CLI 据此打印，退出码不受降级影响）。"""

    run_id: str = ""
    mode: str = "strong"
    requested: int = 0
    analyzed: int = 0
    blocked: int = 0
    skipped: int = 0
    llm_calls: int = 0
    cost_cny: float = 0.0
    fulltext_hits: int = 0
    degraded: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False


def _needs_analysis(paper: Paper, *, force: bool) -> bool:
    if force:
        return True
    if not paper.deep_analysis_md:
        return True
    return paper.deep_content_hash != paper.content_hash


def _watched(paper: Paper, watchlist: WatchlistConfig) -> bool:
    return match_watchlist(watchlist, paper.title, paper.abstract or "").matched


def _select(
    session: Session,
    *,
    ids: list[str] | None,
    watchlist: WatchlistConfig,
    limit: int,
    a_cap: int,
    force: bool,
) -> tuple[list[Paper], int]:
    papers = list(session.exec(select(Paper)).all())
    by_id = {p.id: p for p in papers}

    if ids:
        chosen = [by_id[i] for i in ids if i in by_id]
        eligible = list(chosen)
        chosen = [p for p in chosen if _needs_analysis(p, force=force)]
        return chosen[:limit], len(eligible) - len(chosen)

    pool = [
        p
        for p in papers
        if p.signal_state not in (SIGNAL_REJECTED, SIGNAL_SAMPLE)
        and (_watched(p, watchlist) or p.tier == 1)
    ]
    skipped = sum(1 for p in pool if not _needs_analysis(p, force=force))
    candidates = [p for p in pool if _needs_analysis(p, force=force)]

    # 通道 A：watchlist 命中（按命中分降序），最多 a_cap 篇
    a_hits = [(p, match_watchlist(watchlist, p.title, p.abstract or "").score) for p in candidates]
    a_hits = [item for item in a_hits if _watched(item[0], watchlist)]
    a_hits.sort(key=lambda t: (t[1], t[0].first_seen_at or ""), reverse=True)
    chosen = [p for p, _ in a_hits[:a_cap]]

    # 通道 B：Tier 1 补读，填满 limit
    if len(chosen) < limit:
        chosen_ids = {p.id for p in chosen}
        backlog = [p for p in candidates if p.tier == 1 and p.id not in chosen_ids]
        backlog.sort(key=lambda p: ((p.citation_count or 0), p.first_seen_at or ""), reverse=True)
        chosen.extend(backlog[: limit - len(chosen)])
    return chosen, skipped


def _weekly_calls(session: Session, *, today: date) -> int:
    start = window_starts(today)["weekly"]
    rows = session.exec(select(LLMUsage).where(LLMUsage.purpose == "deep_analysis")).all()
    return sum(
        1
        for row in rows
        if row.created_at and row.created_at >= start.isoformat(timespec="seconds")
    )


async def run_deep_analysis(
    *,
    ids: list[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    today: date | None = None,
    settings: Settings | None = None,
    db_path: Path | None = None,
    client: LLMClient | NullLLM | None = None,
    http_client: HttpClient | None = None,
    watchlist: WatchlistConfig | None = None,
) -> DeepAnalysisReport:
    """跑一次深度分析。★ off 档直接返回；失败只 degraded，不影响退出码。"""
    settings = settings or load_settings()
    report = DeepAnalysisReport(dry_run=dry_run)
    report.run_id = uuid.uuid4().hex[:12]
    day = today or today_local()

    if settings.effective_mode() == RunMode.OFF:
        report.mode = "off"
        return report

    db_file = Path(db_path) if db_path else default_db_path(settings)
    engine = create_engine(f"sqlite:///{db_file}", echo=False)
    SQLModel.metadata.create_all(engine)

    effective_limit = limit if limit is not None else settings.deep_analysis.default_limit
    a_cap = min(settings.collect.deep_dive_top_n, effective_limit)
    weekly_limit = settings.deep_analysis.weekly_limit
    lens = settings.deep_analysis.lens
    provider = settings.llm.provider
    model = settings.llm.strong_model
    max_tokens = settings.llm.max_tokens_strong

    wl = watchlist or load_watchlist()
    owns_client = client is None
    llm = client or build_client(settings, tier="strong")
    owns_http = http_client is None
    http = http_client or client_from_settings(settings)
    calls = 0
    week_calls = 0

    try:
        with Session(engine) as session:
            week_calls = _weekly_calls(session, today=day)
            candidates, skipped = _select(
                session,
                ids=ids,
                watchlist=wl,
                limit=effective_limit,
                a_cap=a_cap,
                force=force,
            )
            report.requested = len(candidates)
            report.skipped = skipped

            for i, paper in enumerate(candidates):
                if week_calls >= weekly_limit:
                    report.blocked = len(candidates) - i
                    report.degraded["quota"] = f"周配额已满（{weekly_limit} 篇）"
                    break

                fulltext = None
                if not dry_run and paper.arxiv_id:
                    fulltext = await fetch_arxiv_html(
                        http,
                        paper.arxiv_id,
                        max_chars=settings.deep_analysis.max_fulltext_chars,
                    )
                if fulltext:
                    report.fulltext_hits += 1

                days_since = age_days_since(paper.published_at, day)
                prompt = render_prompt(
                    "deep_analysis.j2",
                    paper_id=paper.id,
                    title=paper.title,
                    authors_and_affiliations=authors_text(paper),
                    year=year_text(paper),
                    venue=paper.venue or "—",
                    abstract=paper.abstract or "",
                    references=references_text(session, paper.id),
                    citation_count=paper.citation_count or 0,
                    days_since_published=days_since if days_since is not None else "—",
                    citing_titles=None,
                    has_fulltext=fulltext is not None,
                    fulltext=fulltext or "",
                    lens_profile=lens.profile,
                    lens_column_header=lens.column_header,
                    lens_q1_hint=lens.q1_hint,
                    lens_q2_hint=lens.q2_hint,
                    lens_q3_hint=lens.q3_hint,
                    lens_q4_hint=lens.q4_hint,
                    lens_q5_hint=lens.q5_hint,
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
                week_calls += 1
                if dry_run:
                    report.analyzed += 1
                    report.llm_calls += 1
                    report.cost_cny += estimate.cost_cny
                    continue

                try:
                    response = await llm.complete(
                        [{"role": "user", "content": prompt}], json_mode=False
                    )
                except LLMError as exc:
                    paper.deep_status = "failed"
                    report.degraded["llm"] = f"{type(exc).__name__}: {exc}"
                    continue

                # ★ 真实调用即记账（即使输出校验失败，成本已发生）
                analyze_cost = actual_cost(settings, provider, model, response)
                session.add(
                    LLMUsage(
                        run_id=report.run_id,
                        model=model,
                        tokens_in=response.tokens_in,
                        tokens_out=response.tokens_out,
                        cost_cny=analyze_cost,
                        cache_hit=response.cache_hit,
                        purpose="deep_analysis",
                    )
                )
                report.llm_calls += 1
                report.cost_cny += analyze_cost

                content = response.content.strip()
                if not content or paper.id not in content:
                    paper.deep_status = "failed"
                    report.degraded["llm"] = "输出为空或 paper_id 未回显（下次重试）"
                    continue

                paper.deep_analysis_md = content
                paper.deep_analyzed_at = day.isoformat()
                paper.deep_content_hash = paper.content_hash
                paper.deep_status = "done"
                report.analyzed += 1

            if not dry_run:
                session.commit()
    finally:
        if owns_client:
            await llm.aclose()
        if owns_http:
            await http.aclose()

    return report
