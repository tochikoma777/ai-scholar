"""L5a 编排：读 public 数据 → 组装 `SiteData` → 渲染单文件 → 原子写。

★ 输入只有三处，**绝不读 vault**：
  1. `data/reports/*.json`（每日报告）
  2. `data/predictions/scoreboard.jsonl`（记分板）
  3. `db` 里的论文元数据（标题 / 摘要级增强文本 / 分项分）

★ 零网络、零 LLM、零新增依赖；`off` 档同样可跑。
★ 输出确定性：页面不含生成时间，重跑逐字节一致（`--check` 可断言）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlmodel import Session, func, select

from ai_scholar.config import Settings, load_settings, load_watchlist, project_root
from ai_scholar.export.html import (
    HtmlExporter,
    SiteCalibrationRow,
    SiteCostPoint,
    SiteData,
    SitePaper,
    SitePrediction,
    SiteTrendRow,
)
from ai_scholar.pipeline.export import select_papers
from ai_scholar.pipeline.report import DailyReport, PredictionBrief
from ai_scholar.pipeline.score import score_many
from ai_scholar.pipeline.stats import CalibrationReport, summarize
from ai_scholar.pipeline.stats import calibration as run_calibration
from ai_scholar.store.db import engine_for
from ai_scholar.store.models import Paper
from ai_scholar.utils.errors import ConfigError
from ai_scholar.utils.timeutil import today_local

_DATE_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


@dataclass
class SiteReport:
    """`site` 命令的产出（供 CLI 展示）。"""

    out_path: Path
    date: str = ""
    written: bool = False
    changed: bool = False
    size_bytes: int = 0
    papers: int = 0
    reports: int = 0
    degraded: dict[str, str] = field(default_factory=dict)


def resolve_site_dir(out: Path | str | None, settings: Settings) -> Path:
    """输出目录：`--out` > `export.site_dir`；`~` 展开，相对路径按仓库根解析。"""
    raw = str(out) if out is not None else settings.export.site_dir
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_root() / path
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise ConfigError(f"site 输出目录被同名文件占用：{path}")
    return path


def load_reports(data_dir: Path, *, days: int) -> tuple[list[DailyReport], int]:
    """读最近的报告 JSON（按日期倒序）；返回 `(报告, 解析失败数)`。

    ★ 坏文件只计数并暴露，不静默当成"没有报告"。
    """
    report_dir = Path(data_dir) / "reports"
    if not report_dir.is_dir():
        return [], 0
    dated: list[tuple[str, Path]] = []
    for path in report_dir.glob("*.json"):
        match = _DATE_FILE.match(path.name)
        if match:
            dated.append((match.group(1), path))
    dated.sort(key=lambda item: item[0], reverse=True)

    reports: list[DailyReport] = []
    broken = 0
    for _day, path in dated[:days]:
        try:
            reports.append(DailyReport.model_validate_json(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError):
            broken += 1
    return reports, broken


def _prediction_items(
    report: DailyReport | None,
) -> tuple[list[SitePrediction], list[SitePrediction], list[SitePrediction]]:
    if report is None:
        return [], [], []

    def convert(items: list[PredictionBrief]) -> list[SitePrediction]:
        return [
            SitePrediction(
                claim=item.claim,
                status=item.status,
                due_at=item.due_at,
                observed=item.observed,
                threshold=item.threshold,
            )
            for item in items
        ]

    return (
        convert(report.predictions_new),
        convert(report.predictions_verified),
        convert(report.predictions_due),
    )


def _calibration_section(settings: Settings, *, day: date) -> tuple[CalibrationReport | None, str]:
    """返回 `(校准结果, 降级原因)`；db 缺失是降级，不是错误。"""
    db_file = settings.db_path()
    if not db_file.exists():
        return None, "数据库不存在（跳过定级校准与论文列表）"
    engine = engine_for(db_file)
    with Session(engine) as session:
        return run_calibration(session, settings.calibration, today=day), ""


def _site_papers(settings: Settings, *, limit: int) -> tuple[list[SitePaper], int]:
    """返回 `(页面论文, 库内总数)`；db 缺失 ⇒ `([], 0)`。"""
    db_file = settings.db_path()
    if not db_file.exists():
        return [], 0
    watchlist = load_watchlist()
    engine = engine_for(db_file)
    with Session(engine) as session:
        total = int(session.exec(select(func.count()).select_from(Paper)).one())
        papers = select_papers(session, limit=limit, all_papers=True)
        breakdowns = score_many(papers, watchlist)
    items: list[SitePaper] = []
    for paper in papers:
        score = breakdowns[paper.id]
        items.append(
            SitePaper(
                paper_id=paper.id,
                title=paper.title,
                url=paper.url,
                first_seen=(paper.first_seen_at or "")[:10],
                tier=paper.tier,
                groups=score.groups,
                one_line=paper.one_line,
                why=paper.why,
                heat=score.heat,
                cross_source=score.cross_source,
                topic=score.topic,
            )
        )
    return items, total


def build_site_data(
    *,
    settings: Settings,
    days: int,
    limit: int,
    today: date | None = None,
) -> SiteData:
    """聚合三类输入（★ 只读）。缺什么就降级什么，不伪造。"""
    day = today or today_local()
    reports, broken = load_reports(settings.data_dir(), days=days)
    latest = reports[0] if reports else None
    degraded: dict[str, str] = {}
    if latest is None:
        degraded["reports"] = (
            f"{broken} 个报告文件解析失败（无可用报告）"
            if broken
            else "无 data/reports/*.json（先跑 `ai-scholar report`）"
        )
    elif broken:
        degraded["reports"] = f"{broken} 个报告文件解析失败（已跳过）"
    if latest is not None:
        # ★ 报告自身的降级（HF 400 / S2 429 等）必须出现在页面 —— 这是"静默失败"的
        #   第一现场。只显示"输入缺失"会把「源在报错但报告照样生成」的情况藏起来。
        degraded.update(latest.degraded)

    summary = summarize(settings=settings, today=day)
    calibration, calibration_error = _calibration_section(settings, day=day)
    if calibration_error:
        degraded["db"] = calibration_error

    new, verified, due = _prediction_items(latest)
    papers, db_total = _site_papers(settings, limit=limit)
    # ★ L8-F：成本趋势（旧→新）+ 本月累计 + 异常（异常字段来自报告 JSON）
    cost_series = [
        SiteCostPoint(date=report.date, cost=float(report.cost_cny or 0.0))
        for report in reversed(reports)
    ]
    cost_month = round(
        sum(point.cost for point in cost_series if point.date[:7] == day.isoformat()[:7]), 4
    )
    return SiteData(
        # ★ 无报告时用固定文案而不是 `today()`：否则同一份输入过一天就产生 diff
        date=latest.date if latest else "（无报告）",
        total_papers=latest.total_papers if latest else db_total,
        new_today=latest.new_today if latest else 0,
        hits_today=len(latest.watchlist_hits) if latest else 0,
        cost_cny=latest.cost_cny if latest and latest.cost_cny is not None else 0.0,
        cost_series=cost_series,
        cost_month=cost_month,
        cost_anomalies=list(latest.cost_anomalies) if latest else [],
        open=summary.open,
        hit=summary.hit,
        miss=summary.miss,
        insufficient=summary.insufficient,
        hit_rate=summary.hit_rate,
        opinions=summary.opinions,
        predictions_new=new,
        predictions_due=due,
        predictions_verified=verified,
        calibration_rows=[
            SiteCalibrationRow(
                tier=row.tier,
                n=row.n,
                mean_citations=row.mean_citations,
                median_citations=row.median_citations,
            )
            for row in (calibration.rows if calibration else [])
        ],
        spearman=calibration.spearman if calibration else None,
        calibration_enough=calibration.enough if calibration else False,
        calibration_reason=calibration.reason if calibration else "数据库不存在",
        papers=papers,
        trend=[
            SiteTrendRow(
                date=report.date,
                new_today=report.new_today,
                hits=len(report.watchlist_hits),
                verified=len(report.predictions_verified),
            )
            for report in reversed(reports)
        ],
        degraded=degraded,
    )


def _matches(path: Path, content: str) -> bool:
    try:
        return path.read_bytes() == content.encode("utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return False


def run_site(
    *,
    out: Path | str | None = None,
    days: int | None = None,
    limit: int | None = None,
    settings: Settings | None = None,
    today: date | None = None,
    check: bool = False,
) -> SiteReport:
    """生成 `index.html`（默认 `~/projects/ai-scholar-site/`）。

    ★ `check=True` 只逐字节比对、不写盘：有变更 `changed=True`，由 CLI 决定退出码。
    """
    settings = settings or load_settings()
    days = settings.export.site_days if days is None else days
    limit = settings.export.site_max_papers if limit is None else limit
    if days <= 0:
        raise ConfigError(f"`--days` 必须 > 0（当前 {days}）")
    if limit <= 0:
        raise ConfigError(f"`--limit` 必须 > 0（当前 {limit}）")

    data = build_site_data(settings=settings, days=days, limit=limit, today=today)
    exporter = HtmlExporter()
    content = exporter.render_site(data)
    out_dir = resolve_site_dir(out, settings)
    out_path = exporter.site_path(out_dir)

    report = SiteReport(
        out_path=out_path,
        date=data.date,
        size_bytes=len(content.encode("utf-8")),
        papers=len(data.papers),
        reports=len(data.trend),
        degraded=dict(data.degraded),
    )
    report.changed = not _matches(out_path, content)
    if check:
        return report
    exporter.write_site(out_dir, content)
    report.written = True
    return report
