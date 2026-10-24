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
from ai_scholar.store.models import Paper, Run
from ai_scholar.utils.timeutil import day_window

SCHEMA_VERSION = 1
WATCHLIST_TOP_N = 10
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "report"


class ReportHit(BaseModel):
    """列表项（★ 命中 / 新定级 / 深度分析 共用结构）。"""

    id: str
    title: str
    score: float = 0.0  # ★ watchlist 命中分（只用于排序，不是门槛）
    groups: list[str] = Field(default_factory=list)


class RunStatus(BaseModel):
    """今日某次 run 的运行状态（来自 `runs` 表）。"""

    run_id: str
    mode: str
    source: str | None = None
    fetched: int = 0
    new: int = 0
    degraded: list[str] = Field(default_factory=list)


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


def build_daily_report(
    session: Session,
    *,
    today: date,
    watchlist: WatchlistConfig,
    top_n: int = WATCHLIST_TOP_N,
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

    return DailyReport(
        date=day,
        total_papers=total,
        new_today=len(new_papers),
        watchlist_hits=hits,
        new_graded=new_graded,
        runs=runs,
        degraded=degraded,
    )


def render_markdown(report: DailyReport) -> str:
    """用 Jinja 模板渲染 md（★ 调格式只改 .j2）。"""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
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
