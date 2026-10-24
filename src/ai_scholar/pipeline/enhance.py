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
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from sqlmodel import Session, SQLModel, col, create_engine, select

from ai_scholar.budget import BudgetState, check_budget, estimate_cost
from ai_scholar.config import RunMode, Settings, load_settings, load_watchlist, match_watchlist
from ai_scholar.llm import LLMClient, NullLLM, build_client, parse_json_content
from ai_scholar.models import WatchlistConfig
from ai_scholar.store.db import default_db_path
from ai_scholar.store.models import LINK_TYPE_SIMILARITY, LLMUsage, Paper, PaperLink
from ai_scholar.utils.errors import LLMError
from ai_scholar.utils.timeutil import today_local, tz

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_ENV = Environment(loader=FileSystemLoader(str(_PROMPT_DIR)), autoescape=False)

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


def _render_prompt(name: str, **kwargs: Any) -> str:
    return _ENV.get_template(name).render(**kwargs)


def _estimate_input_tokens(text: str) -> int:
    """调用前的输入 token 粗估（保守：英中混合按 3 字符/token）。"""
    return max(1, len(text) // 3)


def _authors(paper: Paper) -> str:
    return ", ".join(json.loads(paper.authors_json or "[]"))


def _year(paper: Paper) -> str:
    return (paper.published_at or "")[:4] or "—"


def _window_starts(day: date) -> dict[str, datetime]:
    """日/周/月/年窗口起点（本地 Asia/Shanghai 起算 → UTC）。"""
    zone = tz()
    day_start = datetime.combine(day, time.min, tzinfo=zone)
    week_start = datetime.combine(day - timedelta(days=day.weekday()), time.min, tzinfo=zone)
    month_start = datetime.combine(day.replace(day=1), time.min, tzinfo=zone)
    year_start = datetime.combine(day.replace(month=1, day=1), time.min, tzinfo=zone)
    return {
        "daily": day_start.astimezone(UTC),
        "weekly": week_start.astimezone(UTC),
        "monthly": month_start.astimezone(UTC),
        "yearly": year_start.astimezone(UTC),
    }


def spent_in_windows(session: Session, *, today: date, calls_this_run: int = 0) -> BudgetState:
    """从 `llm_usage` 汇总各窗口已花费（成本预算 §3.4 记账闭环）。"""
    starts = _window_starts(today)
    spent = {"daily": 0.0, "weekly": 0.0, "monthly": 0.0, "yearly": 0.0}
    for row in session.exec(select(LLMUsage)).all():
        if row.cost_cny is None or not row.created_at:
            continue
        for key, start in starts.items():
            if row.created_at >= start.isoformat(timespec="seconds"):
                spent[key] += row.cost_cny
    return BudgetState(
        spent_daily=spent["daily"],
        spent_weekly=spent["weekly"],
        spent_monthly=spent["monthly"],
        spent_yearly=spent["yearly"],
        calls_this_run=calls_this_run,
    )


def _select_candidates(
    session: Session,
    *,
    today: date,
    limit: int,
    report: EnhanceReport,
    watchlist: WatchlistConfig | None = None,
) -> list[tuple[Paper, float]]:
    """watchlist 命中的未增强论文，按 (命中分, first_seen) 降序。"""
    watchlist = watchlist or load_watchlist()
    candidates: list[tuple[Paper, float]] = []
    for paper in session.exec(select(Paper)).all():
        hit = match_watchlist(watchlist, paper.title, paper.abstract or "")
        if not hit.matched:
            continue
        if (
            paper.enhance_content_hash is not None
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
            )
            report.requested = len(candidates)
            by_id = {p.id: p for p in session.exec(select(Paper)).all()}

            for i, (paper, _score) in enumerate(candidates):
                prompt = _render_prompt(
                    "enhance.j2",
                    paper_id=paper.id,
                    title=paper.title,
                    abstract=paper.abstract or "",
                    authors_and_affiliations=_authors(paper),
                    year=_year(paper),
                    venue=paper.venue or "—",
                )
                estimate = estimate_cost(
                    settings.budget,
                    provider,
                    model,
                    _estimate_input_tokens(prompt),
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
                    data = parse_json_content(response.content)
                except LLMError as exc:
                    report.degraded["llm"] = f"{type(exc).__name__}: {exc}"
                    continue
                if data.get("paper_id") != paper.id:
                    report.degraded["llm"] = f"paper_id 回显不一致：{data.get('paper_id')!r}"
                    continue

                paper.one_line = data.get("one_line") or None
                paper.why = data.get("why") or None
                paper.method_tags_json = json.dumps(
                    data.get("method_tags") or [], ensure_ascii=False
                )
                paper.enhanced_at = day.isoformat()
                paper.enhance_content_hash = paper.content_hash
                session.add(
                    LLMUsage(
                        run_id=report.run_id,
                        model=model,
                        tokens_in=response.tokens_in,
                        tokens_out=response.tokens_out,
                        cost_cny=estimate.cost_cny,
                        cache_hit=response.cache_hit,
                        purpose="enhance",
                    )
                )
                report.enhanced += 1
                report.llm_calls += 1
                report.cost_cny += estimate.cost_cny

            # 关联解读（强相似边）
            for edge in _select_links(
                session, strong_weight=strong_weight, limit=link_pairs_limit, force=force
            ):
                paper_a = by_id.get(edge.src_id)
                paper_b = by_id.get(edge.dst_id)
                if paper_a is None or paper_b is None:
                    continue
                prompt = _render_prompt(
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
                    _estimate_input_tokens(prompt),
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
                    data = parse_json_content(response.content)
                except LLMError as exc:
                    report.degraded["link"] = f"{type(exc).__name__}: {exc}"
                    continue
                edge.interpretation = data.get("interpretation") or None
                session.add(
                    LLMUsage(
                        run_id=report.run_id,
                        model=model,
                        tokens_in=response.tokens_in,
                        tokens_out=response.tokens_out,
                        cost_cny=estimate.cost_cny,
                        cache_hit=response.cache_hit,
                        purpose="enhance_link",
                    )
                )
                report.links_interpreted += 1
                report.llm_calls += 1
                report.cost_cny += estimate.cost_cny

            if not dry_run:
                session.commit()
    finally:
        if owns_client:
            await llm.aclose()

    return report
