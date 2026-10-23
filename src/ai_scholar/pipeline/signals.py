"""时序采样状态机（§3.7.2）。

★ 状态**持久化**在 `papers.signal_state / signal_next_at` —— 绝不每日重算：
  若每日重算集合，新论文只会有一个快照，永远没有增长曲线，C8 延迟定级被架空。

★ 两套时钟，混用必错：

| 时钟 | 字段 | 用于 |
|---|---|---|
| 成熟度 | `published_at` | 快路径、dormant 复活点、定级门槛 |
| 观测窗口 | `first_seen` | pending 的 90 天退出、留样集保留期 |

★ dormant 复活：`published_at + 180` 天单点采样一次；**唯一**条件是
  `citation_count ≥ min_citations`。age 只决定"何时采"，不参与"是否复活"
  （否则采样点 age 恒 ≥180 ⇒ 全量复活）。

★ 幂等：`signals` 主键 `(paper_id, observed_at)`，同一天跑两次不产生第二行；
  JSONL 只在**新增**行时追加（`data/signals/YYYY-MM.jsonl`，public 资产）。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import func
from sqlmodel import Session, SQLModel, col, create_engine, select

from ai_scholar.collectors.s2 import S2Record, fetch_s2_papers
from ai_scholar.config import Settings, SignalsSettings, load_settings
from ai_scholar.pipeline.prescreen import apply_prescreen
from ai_scholar.store.db import default_db_path
from ai_scholar.store.models import (
    SIGNAL_DORMANT,
    SIGNAL_PENDING,
    SIGNAL_READY,
    SIGNAL_REJECTED,
    SIGNAL_SAMPLE,
    SIGNAL_SOURCE_S2,
    SIGNAL_TRACKED,
    Paper,
    Signal,
)
from ai_scholar.utils.errors import SourceError
from ai_scholar.utils.http import HttpClient
from ai_scholar.utils.timeutil import age_days_since, parse_iso_date, today_local

SAMPLED_STATES = (SIGNAL_PENDING, SIGNAL_TRACKED, SIGNAL_DORMANT, SIGNAL_SAMPLE)
ALL_STATES = (
    SIGNAL_PENDING,
    SIGNAL_READY,
    SIGNAL_TRACKED,
    SIGNAL_DORMANT,
    SIGNAL_SAMPLE,
    SIGNAL_REJECTED,
)


@dataclass
class SignalsReport:
    run_id: str = ""
    today: str = ""
    backfilled: int = 0
    due: int = 0
    fetched: int = 0
    rows_new: int = 0
    rows_existing: int = 0
    events: dict[str, int] = field(default_factory=dict)
    states: dict[str, int] = field(default_factory=dict)
    degraded: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False
    jsonl_path: str = ""
    duration_s: float = 0.0


def s2_id_for(paper: Paper) -> str | None:
    """采样用的 S2 id（`ARXIV:xxx` / `DOI:xxx`）。"""
    if paper.arxiv_id:
        return f"ARXIV:{paper.arxiv_id}"
    if paper.doi:
        return f"DOI:{paper.doi}"
    return None


def jsonl_path_for(data_dir: Path, day: date) -> Path:
    return Path(data_dir) / "signals" / f"{day:%Y-%m}.jsonl"


def append_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    """追加到月度 JSONL（★ 只在 signals 行**新增**时调用，保证幂等）。"""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def select_due(session: Session, *, today: date, limit: int, force: bool = False) -> list[Paper]:
    """取本次该观测的论文。

    ★ 依赖持久字段 `signal_next_at`（NULL ⇒ 不再常规观测）。
    """
    statement = select(Paper).where(col(Paper.signal_state).in_(SAMPLED_STATES))
    if not force:
        # SQLModel 的可空字段在类型层面是 str | None，比较时需要 cast（执行手册：不用 type: ignore）
        next_at = cast(Any, col(Paper.signal_next_at))
        statement = statement.where(next_at <= today.isoformat(), next_at.is_not(None))
    statement = statement.order_by(col(Paper.signal_next_at), col(Paper.first_seen_at))
    rows = list(session.exec(statement).all())
    return rows[:limit] if limit > 0 else rows


def advance(paper: Paper, *, today: date, cfg: SignalsSettings, citations: int | None) -> str:
    """推进一篇论文的状态机，返回本次事件名（用于报告与测试）。"""
    state = paper.signal_state
    first_seen = parse_iso_date(paper.first_seen_at) or today
    published = parse_iso_date(paper.published_at)
    pub_age = age_days_since(paper.published_at, today)

    # ★ S2 没返回（未索引 / 限流）：不推进状态，下次再试，避免把 dormant 误杀
    if citations is None:
        paper.signal_next_at = (today + timedelta(days=cfg.monthly_days)).isoformat()
        return "unresolved"

    if state == SIGNAL_PENDING:
        if pub_age is not None and pub_age >= cfg.maturity_days:
            paper.signal_state = SIGNAL_READY
            paper.signal_next_at = None
            return "ready"
        if (today - first_seen).days >= cfg.pending_days:
            paper.signal_state = SIGNAL_DORMANT
            revived_point = published + timedelta(days=cfg.maturity_days) if published else today
            # ★ 复活点是"published_at + 180"；若已经过了，今天就补一次单点
            paper.signal_next_at = max(revived_point, today).isoformat()
            return "dormant"
        paper.signal_next_at = (today + timedelta(days=cfg.monthly_days)).isoformat()
        return "sampled"

    if state == SIGNAL_READY:
        paper.signal_next_at = None
        return "ready"

    if state == SIGNAL_TRACKED:
        age = (today - first_seen).days
        if age >= cfg.retention_years * 365:
            paper.signal_next_at = None
            return "retired"
        step = cfg.monthly_days if age < cfg.tracked_first_year_days else cfg.quarterly_days
        paper.signal_next_at = (today + timedelta(days=step)).isoformat()
        return "sampled"

    if state == SIGNAL_SAMPLE:
        if (today - first_seen).days >= cfg.sample_retention_days:
            paper.signal_next_at = None
            return "sample_expired"
        paper.signal_next_at = (today + timedelta(days=cfg.quarterly_days)).isoformat()
        return "sampled"

    if state == SIGNAL_DORMANT:
        # ★ 单点复活：无论达标与否，这次判断后都不再常规观测（最多一次）
        if paper.signal_revived:
            paper.signal_next_at = None
            return "dormant_final"
        paper.signal_revived = True
        if citations >= cfg.min_citations:
            paper.signal_state = SIGNAL_PENDING
            paper.signal_next_at = (today + timedelta(days=cfg.monthly_days)).isoformat()
            return "revived"
        paper.signal_state = SIGNAL_DORMANT
        paper.signal_next_at = None
        return "dormant_final"

    paper.signal_next_at = None
    return "noop"


def _write_signal(
    session: Session, *, paper_id: str, today: date, citation: int | None, refs: int | None
) -> bool:
    """写一行 signals（已存在则跳过）。返回是否新增。"""
    observed_at = today.isoformat()
    if session.get(Signal, (paper_id, observed_at)) is not None:
        return False
    session.add(
        Signal(
            paper_id=paper_id,
            observed_at=observed_at,
            citation_count=citation,
            reference_count=refs,
            source=SIGNAL_SOURCE_S2,
        )
    )
    return True


async def run_signals(
    *,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    today: date | None = None,
    settings: Settings | None = None,
    db_path: Path | None = None,
    client: HttpClient | None = None,
) -> SignalsReport:
    """跑一次采样。★ S2 失败只记 `degraded`，不影响退出码。"""
    started = time.monotonic()
    settings = settings or load_settings()
    report = SignalsReport(run_id=uuid.uuid4().hex[:12], dry_run=dry_run)
    day = today or today_local()
    report.today = day.isoformat()
    report.jsonl_path = str(jsonl_path_for(settings.data_dir(), day))

    db_file = Path(db_path) if db_path else default_db_path(settings)
    engine = create_engine(f"sqlite:///{db_file}", echo=False)
    SQLModel.metadata.create_all(engine)

    effective_limit = limit if limit is not None else settings.signals.default_limit
    cfg = settings.signals
    prescreen_cfg = settings.prescreen

    with Session(engine) as session:
        # ★ 历史行（signal_state IS NULL）先补跑零成本预筛，再谈采样
        legacy = list(session.exec(select(Paper).where(col(Paper.signal_state).is_(None))).all())
        for paper in legacy:
            apply_prescreen(paper, prescreen_cfg=prescreen_cfg, signals_cfg=cfg, today=day)
        report.backfilled = len(legacy)

        due = select_due(session, today=day, limit=effective_limit, force=force)
        report.due = len(due)

        id_to_paper: dict[str, Paper] = {}
        ordered_ids: list[str] = []
        for paper in due:
            s2_id = s2_id_for(paper)
            if s2_id and s2_id not in id_to_paper:
                id_to_paper[s2_id] = paper
                ordered_ids.append(s2_id)

        records_by_id: dict[str, S2Record | None] = {}
        if ordered_ids:
            source = settings.source("semantic_scholar")
            endpoint = (
                source.endpoint if source else ""
            ) or "https://api.semanticscholar.org/graph/v1/paper/batch"
            owns_client = client is None
            http = client or HttpClient(user_agent=settings.http.user_agent)
            try:
                records = await fetch_s2_papers(
                    http,
                    ordered_ids,
                    endpoint=endpoint,
                    api_key=(source.api_key if source else "") or "",
                    batch_size=(source.batch_size if source else 500),
                    rate_limit=(source.rate_limit if source else "1/1s"),
                )
                if len(records) == len(ordered_ids):
                    records_by_id = dict(zip(ordered_ids, records, strict=False))
                else:
                    report.degraded["semantic_scholar"] = "返回条数与请求数不一致"
            except (SourceError, ValueError) as exc:
                report.degraded["semantic_scholar"] = f"{type(exc).__name__}: {exc}"
            finally:
                if owns_client:
                    await http.aclose()

        new_rows: list[dict[str, object]] = []
        for s2_id in ordered_ids:
            paper = id_to_paper[s2_id]
            record = records_by_id.get(s2_id)
            citation = getattr(record, "citation_count", None) if record is not None else None
            refs = getattr(record, "reference_count", None) if record is not None else None
            if record is not None:
                report.fetched += 1

            created = False
            if record is not None and not dry_run:
                created = _write_signal(
                    session, paper_id=paper.id, today=day, citation=citation, refs=refs
                )
                if citation is not None:
                    paper.citation_count = citation
                if refs is not None:
                    paper.refs_count = refs
            elif record is not None and dry_run:
                created = session.get(Signal, (paper.id, day.isoformat())) is None

            if created:
                report.rows_new += 1
                new_rows.append(
                    {
                        "paper_id": paper.id,
                        "observed_at": day.isoformat(),
                        "citation_count": citation,
                        "reference_count": refs,
                        "source": SIGNAL_SOURCE_S2,
                    }
                )
            elif record is not None:
                report.rows_existing += 1

            paper.signal_last_at = day.isoformat()
            event = advance(paper, today=day, cfg=cfg, citations=citation)
            report.events[event] = report.events.get(event, 0) + 1

        if not dry_run:
            session.commit()
            append_jsonl(Path(report.jsonl_path), new_rows)

        session.flush()
        counts = dict(
            session.exec(
                select(Paper.signal_state, func.count()).group_by(Paper.signal_state)
            ).all()
        )
        report.states = {str(k): int(v) for k, v in counts.items()}

    report.duration_s = round(time.monotonic() - started, 2)
    return report
