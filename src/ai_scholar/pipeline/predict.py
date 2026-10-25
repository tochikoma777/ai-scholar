"""L4 推演生成（★ 零 LLM、零网络、可解释）。

只生成**可验证判断**：每条都有量化判据 + 未来到期日，先过
`store/scoreboard.falsifiability_errors()` 才允许进记分板。

三类候选（§7.2 模板）：

- `arxiv_paper_count(direction, threshold)`：最近 N 天新增量 × 增长系数；
- `entity_mentions(entity, threshold)`：方法标签提及量 × 增长系数；
- `citation_reach(paper, threshold)`：当前被引 × 增长系数（长周期）。

★ 用户/LLM 的长期趋势洞察走 `--insight`，落到 `opinions.jsonl`，
**不进记分板、不参与命中率**（§7.1）。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, create_engine, select

from ai_scholar.config import PredictionSettings, Settings, load_settings
from ai_scholar.store.db import default_db_path
from ai_scholar.store.models import Paper
from ai_scholar.store.scoreboard import (
    Criterion,
    Opinion,
    Prediction,
    append_opinion,
    append_snapshot,
    falsifiability_errors,
    load_latest,
    opinion_id,
    opinions_path,
    prediction_id,
    scoreboard_path,
)
from ai_scholar.utils.timeutil import parse_iso_date, today_local


@dataclass
class PredictReport:
    today: str = ""
    scoreboard_path: str = ""
    created: int = 0
    skipped: int = 0
    opinions: int = 0
    dry_run: bool = False
    degraded: dict[str, str] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)
    predictions: list[Prediction] = field(default_factory=list)


def _threshold(baseline: int, factor: float) -> int:
    return max(1, math.ceil(baseline * factor))


def _direction_candidates(
    session: Session, cfg: PredictionSettings, *, today: date
) -> list[tuple[Criterion, str, dict[str, Any]]]:
    start = (today - timedelta(days=cfg.direction_window_days)).isoformat()
    papers = session.exec(select(Paper).where(col(Paper.first_seen_at) >= start)).all()
    counts: Counter[str] = Counter()
    for paper in papers:
        category = (paper.primary_category or "").strip()
        if category:
            counts[category] += 1
    due = (today + timedelta(days=cfg.direction_horizon_days)).isoformat()
    out: list[tuple[Criterion, str, dict[str, Any]]] = []
    for direction, baseline in counts.most_common():
        if baseline < cfg.min_direction_papers:
            continue
        threshold = _threshold(baseline, cfg.direction_growth_factor)
        criterion = Criterion(
            type="arxiv_paper_count",
            target=direction,
            threshold=threshold,
            window_days=cfg.direction_horizon_days,
        )
        claim = (
            f"方向 {direction} 在 {due} 前新增论文 ≥ {threshold} 篇"
            f"（最近 {cfg.direction_window_days} 天观测 {baseline} 篇）"
        )
        out.append(
            (
                criterion,
                claim,
                {
                    "baseline_count": baseline,
                    "baseline_days": cfg.direction_window_days,
                    "growth_factor": cfg.direction_growth_factor,
                },
            )
        )
    return out


def _entity_candidates(
    session: Session, cfg: PredictionSettings, *, today: date
) -> list[tuple[Criterion, str, dict[str, Any]]]:
    start = (today - timedelta(days=cfg.entity_window_days)).isoformat()
    papers = session.exec(
        select(Paper).where(
            col(Paper.first_seen_at) >= start,
            col(Paper.method_tags_json).is_not(None),
        )
    ).all()
    counts: Counter[str] = Counter()
    for paper in papers:
        tags = _tags_of(paper)
        for tag in set(tags):
            counts[tag] += 1

    due = (today + timedelta(days=cfg.entity_horizon_days)).isoformat()
    out: list[tuple[Criterion, str, dict[str, Any]]] = []
    for tag, baseline in counts.most_common():
        if baseline < cfg.min_entity_mentions:
            continue
        threshold = _threshold(baseline, cfg.entity_growth_factor)
        criterion = Criterion(
            type="entity_mentions",
            target=tag,
            threshold=threshold,
            window_days=cfg.entity_horizon_days,
        )
        claim = (
            f"方法「{tag}」在 {due} 前被 ≥ {threshold} 篇新增论文提及"
            f"（最近 {cfg.entity_window_days} 天 {baseline} 篇）"
        )
        out.append(
            (
                criterion,
                claim,
                {
                    "baseline_count": baseline,
                    "baseline_days": cfg.entity_window_days,
                    "growth_factor": cfg.entity_growth_factor,
                    "basis": "method_tags",
                },
            )
        )
    return out


def _citation_candidates(
    session: Session, cfg: PredictionSettings, *, today: date
) -> list[tuple[Criterion, str, dict[str, Any]]]:
    cutoff = today - timedelta(days=30)
    rows = session.exec(
        select(Paper)
        .where(col(Paper.citation_count) >= cfg.citation_min_current)
        .order_by(col(Paper.citation_count).desc())
        .limit(cfg.max_new_per_run * 3)
    ).all()
    due = (today + timedelta(days=cfg.citation_horizon_days)).isoformat()
    out: list[tuple[Criterion, str, dict[str, Any]]] = []
    for paper in rows:
        published = parse_iso_date(paper.published_at)
        if published is None or published > cutoff:
            continue
        current = paper.citation_count or 0
        threshold = _threshold(current, cfg.citation_growth_factor)
        criterion = Criterion(
            type="citation_reach",
            target=paper.id,
            threshold=threshold,
            window_days=cfg.citation_horizon_days,
        )
        title = paper.title if len(paper.title) <= 48 else paper.title[:47] + "…"
        claim = f"论文「{title}」在 {due} 前被引 ≥ {threshold} 次（当前 {current} 次）"
        out.append(
            (
                criterion,
                claim,
                {
                    "baseline_count": current,
                    "growth_factor": cfg.citation_growth_factor,
                    "title": paper.title,
                },
            )
        )
    return out


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


def _round_robin(
    buckets: list[list[tuple[Criterion, str, dict[str, Any]]]], limit: int
) -> list[tuple[Criterion, str, dict[str, Any]]]:
    queues = [list(bucket) for bucket in buckets]
    out: list[tuple[Criterion, str, dict[str, Any]]] = []
    while len(out) < limit and any(queues):
        for queue in queues:
            if queue and len(out) < limit:
                out.append(queue.pop(0))
    return out


def run_predict(
    *,
    settings: Settings | None = None,
    db_path: Path | None = None,
    today: date | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    insights: list[str] | None = None,
) -> PredictReport:
    settings = settings or load_settings()
    day = today or today_local()
    cfg = settings.predictions
    report = PredictReport(
        today=day.isoformat(),
        scoreboard_path=str(scoreboard_path(settings.data_dir())),
        dry_run=dry_run,
    )

    # ① 观点先落盘：永不进记分板，也不等待 DB
    for text in insights or []:
        text = text.strip()
        if not text:
            continue
        opinion = Opinion(
            id=opinion_id(created_at=day.isoformat(), text=text),
            text=text,
            created_at=day.isoformat(),
        )
        if dry_run or append_opinion(opinions_path(settings.data_dir()), opinion):
            report.opinions += 1

    # ② 生成候选（只读 DB；db 不存在 ⇒ 显式降级，不假装没有论文）
    db_file = Path(db_path) if db_path else default_db_path(settings)
    if not db_file.exists():
        report.degraded["db"] = f"数据库不存在：{db_file}"
        return report

    engine = create_engine(f"sqlite:///{db_file}", echo=False)
    with Session(engine) as session:
        buckets = [
            _direction_candidates(session, cfg, today=day),
            _entity_candidates(session, cfg, today=day),
            _citation_candidates(session, cfg, today=day),
        ]

    cap = limit if limit is not None else cfg.max_new_per_run
    candidates = _round_robin(buckets, max(0, cap))

    existing = {
        (item.criterion.type, item.criterion.target)
        for item in load_latest(scoreboard_path(settings.data_dir()))
        if item.status == "open"
    }
    path = scoreboard_path(settings.data_dir())
    seen: set[tuple[str, str]] = set()
    for criterion, claim, evidence in candidates:
        key = (criterion.type, criterion.target)
        if key in existing or key in seen:
            report.skipped += 1
            continue
        due_at = (day + timedelta(days=criterion.window_days)).isoformat()
        errors = falsifiability_errors(criterion, due_at=due_at, today=day)
        if errors:
            report.rejected.append(f"{criterion.type}:{criterion.target} — {'；'.join(errors)}")
            continue
        prediction = Prediction(
            id=prediction_id(created_at=day.isoformat(), criterion=criterion, due_at=due_at),
            claim=claim,
            criterion=criterion,
            created_at=day.isoformat(),
            due_at=due_at,
            evidence=evidence,
        )
        seen.add(key)
        report.predictions.append(prediction)
        if dry_run or append_snapshot(path, prediction):
            report.created += 1

    return report
