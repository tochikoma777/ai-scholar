"""L4 统计：到期验证 / 记分板汇总 / 定级校准 / 影响分析。

★ 诚实性硬约束（§7.1）：

- 只有**可验证判断**进统计；`opinions.jsonl` 只计数、**绝不进命中率**；
- `insufficient`（数据不足）**不计入分母**，单独展示，避免用"无法判断"美化分数；
- 校准 / 影响分析在样本不足时**只报数字、不下结论**。
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, create_engine, func, select

from ai_scholar.config import CalibrationSettings, ImpactSettings, Settings, load_settings
from ai_scholar.store.db import default_db_path
from ai_scholar.store.models import Paper, Signal
from ai_scholar.store.scoreboard import (
    Prediction,
    append_snapshot,
    load_latest,
    load_opinions,
    opinions_path,
    scoreboard_path,
)
from ai_scholar.utils.timeutil import parse_iso_date, today_local


@dataclass
class VerifyDetail:
    id: str
    claim: str
    status: str
    observed: int | None
    threshold: int
    note: str | None = None


@dataclass
class VerifyReport:
    checked: int = 0
    hit: int = 0
    miss: int = 0
    insufficient: int = 0
    details: list[VerifyDetail] = field(default_factory=list)
    degraded: dict[str, str] = field(default_factory=dict)


@dataclass
class ScoreboardSummary:
    open: int = 0
    hit: int = 0
    miss: int = 0
    insufficient: int = 0
    hit_rate: float | None = None
    opinions: int = 0
    due_open: int = 0


@dataclass
class CalibrationRow:
    tier: int
    n: int
    mean_citations: float
    median_citations: float


@dataclass
class CalibrationReport:
    cutoff: str = ""
    rows: list[CalibrationRow] = field(default_factory=list)
    spearman: float | None = None
    enough: bool = False
    reason: str = ""


@dataclass
class ImpactRow:
    paper_id: str
    title: str
    snapshots: int
    span_days: int
    first_citations: int
    last_citations: int
    growth: int
    velocity_per_30d: float
    verdict: str


@dataclass
class ImpactReport:
    rows: list[ImpactRow] = field(default_factory=list)
    cohort_size: int = 0
    median_velocity: float | None = None
    enough: bool = False
    reason: str = ""


def _window_papers(session: Session, start: str, end: str) -> list[Paper]:
    return list(
        session.exec(
            select(Paper).where(
                col(Paper.first_seen_at) > start,
                col(Paper.first_seen_at) <= end,
            )
        ).all()
    )


def _db_total(session: Session) -> int:
    return int(session.exec(select(func.count()).select_from(Paper)).one())


def _tags_of(paper: Paper) -> list[str]:
    if not paper.method_tags_json:
        return []
    try:
        raw = json.loads(paper.method_tags_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _contains(text: str, needle: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9]){re.escape(needle.lower())}(?![A-Za-z0-9])"
    return re.search(pattern, text.lower()) is not None


def _evaluate(
    session: Session, prediction: Prediction
) -> tuple[str, int | None, str | None, dict[str, Any]]:
    """返回 `(status, observed, note, evidence)`。"""
    criterion = prediction.criterion
    if _db_total(session) == 0:
        return "insufficient", None, "数据库为空（可丢弃缓存尚未重建）", {}

    start, end = prediction.created_at, prediction.due_at
    papers = _window_papers(session, start, end)
    if not papers:
        return "insufficient", None, "窗口内无采集记录，无法判断", {"window": [start, end]}

    if criterion.type == "arxiv_paper_count":
        observed = sum(1 for paper in papers if (paper.primary_category or "") == criterion.target)
        evidence = {
            "window": [start, end],
            "window_total": len(papers),
            "basis": "primary_category",
        }
    elif criterion.type == "entity_mentions":
        tagged = [paper for paper in papers if paper.method_tags_json]
        if not tagged:
            return (
                "insufficient",
                None,
                "窗口内无增强标签，实体提及不可测",
                {"window": [start, end], "tagged_papers": 0},
            )
        needle = criterion.target.casefold()
        observed = 0
        for paper in tagged:
            if any(tag.casefold() == needle for tag in _tags_of(paper)) or _contains(
                f"{paper.title}\n{paper.abstract or ''}", criterion.target
            ):
                observed += 1
        evidence = {
            "window": [start, end],
            "tagged_papers": len(tagged),
            "basis": "method_tags+title_abstract",
        }
    elif criterion.type == "citation_reach":
        signal = session.exec(
            select(Signal)
            .where(
                Signal.paper_id == criterion.target,
                col(Signal.observed_at) >= prediction.created_at,
                col(Signal.observed_at) <= end,
            )
            .order_by(col(Signal.observed_at).desc())
            .limit(1)
        ).first()
        if signal is None or signal.citation_count is None:
            return (
                "insufficient",
                None,
                "无到期前的 signals 快照，无法判断",
                {"paper_id": criterion.target, "due_at": end},
            )
        observed = signal.citation_count
        evidence = {
            "paper_id": criterion.target,
            "observed_at": signal.observed_at,
            "basis": "signals.citation_count",
        }
    else:  # 理论上被 falsifiability 门禁挡住；保留显式失败而非静默
        return "insufficient", None, f"未知判据类型：{criterion.type}", {}

    status = "hit" if observed >= criterion.threshold else "miss"
    return status, observed, None, evidence


def verify_due(
    *,
    settings: Settings | None = None,
    db_path: Path | None = None,
    today: date | None = None,
) -> VerifyReport:
    """验证所有到期且 `open` 的推演；每条追加一行新快照（幂等）。"""
    settings = settings or load_settings()
    day = today or today_local()
    report = VerifyReport()
    path = scoreboard_path(settings.data_dir())
    predictions = load_latest(path)
    due = [
        item
        for item in predictions
        if item.status == "open" and (parse_iso_date(item.due_at) or day) <= day
    ]
    if not due:
        return report

    db_file = Path(db_path) if db_path else default_db_path(settings)
    if not db_file.exists():
        report.degraded["db"] = f"数据库不存在：{db_file}"
        return report

    engine = create_engine(f"sqlite:///{db_file}", echo=False)
    with Session(engine) as session:
        for prediction in due:
            status, observed, note, evidence = _evaluate(session, prediction)
            detail = VerifyDetail(
                id=prediction.id,
                claim=prediction.claim,
                status=status,
                observed=observed,
                threshold=prediction.criterion.threshold,
                note=note,
            )
            report.details.append(detail)
            report.checked += 1
            if status == "hit":
                report.hit += 1
            elif status == "miss":
                report.miss += 1
            else:
                report.insufficient += 1

            updated = prediction.model_copy(
                update={
                    "status": status,
                    "verified_at": day.isoformat(),
                    "observed": observed,
                    "note": note,
                    "evidence": {**prediction.evidence, "evaluation": evidence},
                }
            )
            append_snapshot(path, updated)
    return report


def summarize(
    *,
    settings: Settings | None = None,
    today: date | None = None,
) -> ScoreboardSummary:
    settings = settings or load_settings()
    day = today or today_local()
    predictions = load_latest(scoreboard_path(settings.data_dir()))
    summary = ScoreboardSummary()
    for item in predictions:
        if item.status == "open":
            summary.open += 1
            if (parse_iso_date(item.due_at) or day) <= day:
                summary.due_open += 1
        elif item.status == "hit":
            summary.hit += 1
        elif item.status == "miss":
            summary.miss += 1
        else:
            summary.insufficient += 1
    decided = summary.hit + summary.miss
    summary.hit_rate = round(summary.hit / decided, 4) if decided else None
    summary.opinions = len(load_opinions(opinions_path(settings.data_dir())))
    return summary


def _average_ranks(values: list[float]) -> list[float]:
    """平均秩（正确处理并列值），用于 Spearman。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = rank
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    rx, ry = _average_ranks(xs), _average_ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((x - mx) * (y - my) for x, y in zip(rx, ry, strict=True))
    dx = sum((x - mx) ** 2 for x in rx) ** 0.5
    dy = sum((y - my) ** 2 for y in ry) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return float(round(num / (dx * dy), 4))


def calibration(
    session: Session,
    cfg: CalibrationSettings,
    *,
    today: date,
) -> CalibrationReport:
    """回看 (tier, 成熟论文**当前**引用)：Tier 越低应引用越高。样本不足只报数。

    ★ 只取 `published_at + maturity_days` 之前的论文；引用是**当前值**，
    不是"半年整"的历史快照（signals 未必覆盖到那一天）。
    """
    cutoff = (today - timedelta(days=cfg.maturity_days)).isoformat()
    papers = session.exec(
        select(Paper).where(
            col(Paper.tier).is_not(None),
            col(Paper.citation_count).is_not(None),
        )
    ).all()
    samples = [
        paper
        for paper in papers
        if (parsed := parse_iso_date(paper.published_at)) is not None
        and parsed <= today - timedelta(days=cfg.maturity_days)
    ]

    report = CalibrationReport(cutoff=cutoff)
    groups: dict[int, list[int]] = {}
    for paper in samples:
        if paper.tier is None or paper.citation_count is None:
            continue
        groups.setdefault(paper.tier, []).append(paper.citation_count)
    for tier in sorted(groups):
        values = groups[tier]
        report.rows.append(
            CalibrationRow(
                tier=tier,
                n=len(values),
                mean_citations=round(statistics.fmean(values), 2),
                median_citations=round(statistics.median(values), 2),
            )
        )

    enough = len(groups) >= 2 and all(
        len(values) >= cfg.min_sample_per_tier for values in groups.values()
    )
    report.enough = enough
    if not enough:
        detail = "、".join(f"Tier{tier} n={len(values)}" for tier, values in sorted(groups.items()))
        report.reason = (
            f"样本不足（每个 Tier 至少 {cfg.min_sample_per_tier} 篇；当前 {detail or '无'}）"
        )
        return report

    xs: list[float] = []
    ys: list[float] = []
    for paper in samples:
        if paper.tier is None or paper.citation_count is None:
            continue
        xs.append(float(paper.tier))
        ys.append(float(paper.citation_count))
    report.spearman = _spearman(xs, ys)
    return report


def impact(
    session: Session,
    cfg: ImpactSettings,
    *,
    limit: int | None = None,
) -> ImpactReport:
    """被引增长曲线 + 开创/跟风**候选**判断；样本不足只报数字。"""
    ids = session.exec(
        select(Signal.paper_id)
        .group_by(col(Signal.paper_id))
        .having(func.count() >= cfg.min_snapshots)
    ).all()
    rows: list[ImpactRow] = []
    for paper_id in ids:
        paper = session.get(Paper, paper_id)
        if paper is None:
            continue
        signals = session.exec(
            select(Signal).where(Signal.paper_id == paper_id).order_by(col(Signal.observed_at))
        ).all()
        values = [s for s in signals if s.citation_count is not None]
        if len(values) < cfg.min_snapshots:
            continue
        first, last = values[0], values[-1]
        start = parse_iso_date(first.observed_at)
        end = parse_iso_date(last.observed_at)
        if (
            start is None
            or end is None
            or first.citation_count is None
            or last.citation_count is None
        ):
            continue
        span = (end - start).days
        growth = last.citation_count - first.citation_count
        velocity = round(growth / max(span, 1) * 30.0, 4)
        rows.append(
            ImpactRow(
                paper_id=paper_id,
                title=paper.title,
                snapshots=len(values),
                span_days=span,
                first_citations=first.citation_count,
                last_citations=last.citation_count,
                growth=growth,
                velocity_per_30d=velocity,
                verdict="",
            )
        )

    report = ImpactReport()
    eligible = [row for row in rows if row.span_days >= cfg.min_span_days]
    report.cohort_size = len(eligible)
    if eligible:
        report.median_velocity = round(
            statistics.median(row.velocity_per_30d for row in eligible), 4
        )
    report.enough = len(eligible) >= cfg.min_cohort

    for row in rows:
        if row.span_days < cfg.min_span_days:
            row.verdict = f"样本不足（跨度 {row.span_days} < {cfg.min_span_days} 天）"
        elif not report.enough:
            row.verdict = f"样本不足（同期 {len(eligible)} < {cfg.min_cohort} 篇）"
        elif report.median_velocity is not None and row.velocity_per_30d >= report.median_velocity:
            row.verdict = "高于同期中位数（开创性候选）"
        else:
            row.verdict = "低于同期中位数（跟风候选）"

    rows.sort(key=lambda row: row.velocity_per_30d, reverse=True)
    report.rows = rows[: (limit if limit is not None else cfg.default_limit)]
    if not report.enough:
        report.reason = (
            f"样本不足：跨度 ≥{cfg.min_span_days} 天的论文 {len(eligible)} 篇"
            f"（需 ≥{cfg.min_cohort}），结论不下"
        )
    return report
