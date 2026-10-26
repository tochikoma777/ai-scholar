"""每日报告（设计方案 §6.6）。

★ 两套产物分工：
- **每日报告** `data/reports/YYYY-MM-DD.md` + `.json`（Actions 跑，public）—— 本模块
- **Daily Note** vault `40-日志/`（本地 export）—— L2-C，不在本模块

★ 三层分离（§6.6）：`DailyReport` 模型 + `templates/report/daily.md.j2` + 渲染。
  调格式只改 .j2；加板块（数据已有）模型加字段 + 模板加块；加新报告类型新增渲染器。

★ 本阶段（L2）只填「概览 / ★命中 Top10 / 运行状态」三块；「深度分析 / 新定级」
  是 L3 的能力，字段保留但恒空（渲染"暂无"），**不假装有数据**。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field
from sqlmodel import Session, col, func, select

from ai_scholar.config import match_watchlist
from ai_scholar.models import WatchlistConfig
from ai_scholar.store.models import LLMUsage, Paper, Run
from ai_scholar.store.scoreboard import (
    load_latest,
    load_opinions,
    opinions_path,
    scoreboard_path,
)
from ai_scholar.store.wanted import want_command
from ai_scholar.utils.timeutil import DEFAULT_TZ, day_window, parse_iso_date

SCHEMA_VERSION = 1
WATCHLIST_TOP_N = 10
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "report"


class ReportHit(BaseModel):
    """列表项（★ 命中 / 新定级 / 深度分析 共用结构）。"""

    id: str
    title: str
    score: float = 0.0  # ★ watchlist 命中分（只用于排序，不是门槛）
    groups: list[str] = Field(default_factory=list)

    @property
    def want_cmd(self) -> str:
        """★ L7-6「点读入口」：把这篇标记想读的可复制命令（唯一来源，别在模板里手写）。"""
        return want_command(self.id)


class RunStatus(BaseModel):
    """今日某次 run 的运行状态（来自 `runs` 表）。"""

    run_id: str
    mode: str
    source: str | None = None
    fetched: int = 0
    new: int = 0
    degraded: list[str] = Field(default_factory=list)


class PredictionBrief(BaseModel):
    """报告里的推演摘要（★ 只读记分板 JSONL，不生成新推演）。"""

    id: str
    claim: str
    status: str
    due_at: str
    observed: int | None = None
    threshold: int | None = None


class DailyReport(BaseModel):
    """当日报告（md 与 json 共用同一份数据，§6.6）。

    ★ 只含**数据时间**（date），不含生成时间 —— 当天重跑无 diff（红线 7）。
    """

    date: str  # YYYY-MM-DD（本地日）
    schema_version: int = SCHEMA_VERSION
    # 概览
    total_papers: int = 0
    new_today: int = 0
    # ★ 命中 Top10（今日新增里 watchlist 命中的，按分数 desc）
    watchlist_hits: list[ReportHit] = Field(default_factory=list)
    # L3 才填，本阶段恒空
    deep_analyzed: list[ReportHit] = Field(default_factory=list)
    new_graded: list[ReportHit] = Field(default_factory=list)
    # L4 推演：只读记分板；观点单独计数，★ 不进统计
    predictions_verified: list[PredictionBrief] = Field(default_factory=list)
    predictions_due: list[PredictionBrief] = Field(default_factory=list)
    predictions_new: list[PredictionBrief] = Field(default_factory=list)
    opinions_total: int = 0
    # 运行状态
    runs: list[RunStatus] = Field(default_factory=list)
    degraded: dict[str, str] = Field(default_factory=dict)
    # L3 记账后填（成本预算 §6：报告尾部附成本说明）
    cost_cny: float | None = None


@dataclass
class ReportOutcome:
    """report 命令的产出。"""

    report: DailyReport
    md_path: Path
    json_path: Path


def _prediction_briefs(
    data_dir: Path | None, *, today: date
) -> tuple[list[PredictionBrief], list[PredictionBrief], list[PredictionBrief], int]:
    """读记分板（★ 只读；文件不存在 ⇒ 空，不伪造）。"""
    if data_dir is None:
        return [], [], [], 0
    day = today.isoformat()
    predictions = load_latest(scoreboard_path(data_dir))
    verified: list[PredictionBrief] = []
    due: list[PredictionBrief] = []
    new: list[PredictionBrief] = []
    for item in predictions:
        brief = PredictionBrief(
            id=item.id,
            claim=item.claim,
            status=item.status,
            due_at=item.due_at,
            observed=item.observed,
            threshold=item.criterion.threshold,
        )
        if item.verified_at == day:
            verified.append(brief)
        elif item.status == "open" and (parse_iso_date(item.due_at) or today) <= today:
            due.append(brief)
        elif item.created_at == day:
            new.append(brief)
    opinions = len(load_opinions(opinions_path(data_dir)))
    return verified, due, new, opinions


def _daily_cost(session: Session, *, today: date, timezone: str = DEFAULT_TZ) -> float:
    """当日（本地日窗口）实际 LLM 花费（成本预算 §6：让「实际花费」可见）。

    ★ `llm_usage.created_at` 是 UTC ISO；按本地日窗口过滤，四舍五入到 4 位。
    ★ 没有行 ⇒ 0.0（off 档显示 ¥0，不是「—」）。
    """
    start, end = day_window(today, timezone)
    total = session.exec(
        select(func.sum(col(LLMUsage.cost_cny))).where(
            col(LLMUsage.created_at) >= start.isoformat(timespec="seconds"),
            col(LLMUsage.created_at) < end.isoformat(timespec="seconds"),
        )
    ).one()
    return round(float(total or 0.0), 4)


def build_daily_report(
    session: Session,
    *,
    today: date,
    watchlist: WatchlistConfig,
    top_n: int = WATCHLIST_TOP_N,
    data_dir: Path | None = None,
    timezone: str = DEFAULT_TZ,
) -> DailyReport:
    """聚合库内现状，生成当日报告快照（★ 只读，不写库）。"""
    day = today.isoformat()
    total = session.exec(select(func.count()).select_from(Paper)).one()

    new_papers = list(
        session.exec(
            select(Paper)
            .where(col(Paper.first_seen_at) >= day)
            .order_by(col(Paper.published_at).desc())
        ).all()
    )

    hits: list[ReportHit] = []
    for paper in new_papers:
        hit = match_watchlist(watchlist, paper.title, paper.abstract or "")
        if hit.matched:
            hits.append(
                ReportHit(
                    id=paper.id,
                    title=paper.title,
                    score=hit.score,
                    groups=hit.groups,
                )
            )
    hits.sort(key=lambda h: h.score, reverse=True)
    hits = hits[:top_n]

    start, end = day_window(today)  # 本地日 → UTC 半开区间（库内存 UTC）
    runs: list[RunStatus] = []
    degraded: dict[str, str] = {}
    for run in session.exec(
        select(Run).where(
            col(Run.started_at) >= start.isoformat(timespec="seconds"),
            col(Run.started_at) < end.isoformat(timespec="seconds"),
        )
    ).all():
        reasons: list[str] = []
        degraded_map: dict[str, str] = {}
        if run.degraded_json:
            try:
                degraded_map = json.loads(run.degraded_json)
                reasons = list(degraded_map.values())
            except json.JSONDecodeError:
                reasons = ["degraded_json 解析失败"]
        runs.append(
            RunStatus(
                run_id=run.run_id,
                mode=run.mode,
                source=run.source,
                fetched=run.fetched,
                new=run.new,
                degraded=reasons,
            )
        )
        for key, reason in degraded_map.items():
            degraded.setdefault(key, reason)

    graded_today = list(session.exec(select(Paper).where(col(Paper.graded_at) == day)).all())
    graded_today.sort(key=lambda p: (p.tier or 9, p.title))
    new_graded = [ReportHit(id=p.id, title=p.title, score=0.0, groups=[]) for p in graded_today]

    analyzed_today = list(
        session.exec(select(Paper).where(col(Paper.deep_analyzed_at) == day)).all()
    )
    deep_analyzed = [
        ReportHit(id=p.id, title=p.title, score=0.0, groups=[]) for p in analyzed_today
    ]

    verified, due, new, opinions = _prediction_briefs(data_dir, today=today)
    return DailyReport(
        date=day,
        cost_cny=_daily_cost(session, today=today, timezone=timezone),
        total_papers=total,
        new_today=len(new_papers),
        watchlist_hits=hits,
        deep_analyzed=deep_analyzed,
        new_graded=new_graded,
        predictions_verified=verified,
        predictions_due=due,
        predictions_new=new,
        opinions_total=opinions,
        runs=runs,
        degraded=degraded,
    )


def render_markdown(report: DailyReport) -> str:
    """用 Jinja 模板渲染 md（★ 调格式只改 .j2）。"""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("daily.md.j2")
    return template.render(report=report)


def _atomic_write(path: Path, content: str) -> None:
    """原子写（★ 与 export/base.py 同一机制，防半截文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def write_report(report: DailyReport, out_dir: Path) -> ReportOutcome:
    """写 `YYYY-MM-DD.md` + `.json`（★ json 给机器/AI 工具看，顺带兑现核心判据）。"""
    md_path = out_dir / f"{report.date}.md"
    json_path = out_dir / f"{report.date}.json"
    _atomic_write(md_path, render_markdown(report))
    _atomic_write(json_path, report.model_dump_json(indent=2) + "\n")
    return ReportOutcome(report=report, md_path=md_path, json_path=json_path)
