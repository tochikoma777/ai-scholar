"""周报 / 月报（L9-A，设计方案 §6.6「加新报告类型新增渲染器」）。

★ 纯派生：只读 `data/reports/YYYY-MM-DD.json`（public 真源）重算，**零 LLM、零网络**。
  不读 db、不读 vault —— 累计口径与 `pipeline/cost.py` 一致（报告才是可审计的真源）。

★ 幂等：**不含生成时间**，同一份输入连续两次产出逐字节一致（与 `site` 同口径，红线 7）。

★ 坏文件 / 缺字段**显式返回**（`notes` 里写明），不静默吞掉、绝不补 0 冒充。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from ai_scholar.pipeline.report import DailyReport, atomic_write, render_recap
from ai_scholar.report_paths import iter_daily_jsons

PERIOD_WEEK = "week"
PERIOD_MONTH = "month"


@dataclass(frozen=True)
class PeriodWindow:
    """一个自然周 / 自然月窗口（闭区间）。"""

    period: str
    start: date
    end: date

    @property
    def slug(self) -> str:
        """文件名片段：`weekly-2026-W44` / `monthly-2026-10`。"""
        if self.period == PERIOD_WEEK:
            iso = self.start.isocalendar()
            return f"weekly-{iso[0]}-W{iso[1]:02d}"
        return f"monthly-{self.start.year:04d}-{self.start.month:02d}"

    @property
    def title(self) -> str:
        if self.period == PERIOD_WEEK:
            iso = self.start.isocalendar()
            return f"{iso[0]} 年第 {iso[1]} 周（{self.start} ~ {self.end}）"
        return f"{self.start.year:04d}-{self.start.month:02d}（{self.start} ~ {self.end}）"

    def dates(self) -> list[date]:
        days = (self.end - self.start).days + 1
        return [self.start + timedelta(days=i) for i in range(days)]


def week_window(anchor: date) -> PeriodWindow:
    """锚点日所在的**自然周**（周一 ~ 周日）。"""
    start = anchor - timedelta(days=anchor.weekday())
    return PeriodWindow(PERIOD_WEEK, start, start + timedelta(days=6))


def month_window(anchor: date) -> PeriodWindow:
    """锚点日所在的**自然月**（1 号 ~ 月末）。"""
    start = anchor.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        end = start.replace(month=start.month + 1, day=1) - timedelta(days=1)
    return PeriodWindow(PERIOD_MONTH, start, end)


def previous_period(window: PeriodWindow) -> PeriodWindow:
    """上一周期：周一 → 上一周；1 号 → 上个月。

    ★ 为什么需要：自然周/月**第一天生成当期报告只会得到空表**（W44 在周一才刚开始）。
      自动化必须生成**已结束**的周期。
    """
    day_before = window.start - timedelta(days=1)
    if window.period == PERIOD_WEEK:
        return week_window(day_before)
    return month_window(day_before)


class RecapHit(BaseModel):
    """区间内反复命中的论文（按**出现天数**聚合，不是当天分数）。"""

    id: str
    title: str
    days: int = 0  # 命中的天数
    groups: list[str] = Field(default_factory=list)


class RecapReport(BaseModel):
    """周报 / 月报快照（md 与 json 同源）。"""

    period: str  # week | month
    slug: str  # weekly-2026-W44 / monthly-2026-10
    start: str
    end: str
    days_covered: int = 0  # 有报告的**自然日**数
    days_expected: int = 0  # 窗口内的自然日数
    missing_days: list[str] = Field(default_factory=list)
    # 采集
    new_papers: int = 0
    runs_total: int = 0
    # 命中
    top_hits: list[RecapHit] = Field(default_factory=list)
    # LLM 产物（日报里的当日产物累加）
    graded_total: int = 0
    graded_by_tier: dict[str, int] = Field(default_factory=dict)
    deep_analyzed_total: int = 0
    # 成本（真源 = 各日报的 cost_cny）
    cost_total: float = 0.0
    cost_days: int = 0  # 有成本数字的天数（cost_cny 非 null）
    cost_peak_day: str | None = None
    cost_peak_cny: float | None = None
    anomaly_days: list[str] = Field(default_factory=list)
    # 推演（只统计已验证项；insufficient 不进分母，与 stats 同口径）
    predictions_hit: int = 0
    predictions_miss: int = 0
    # 降级：key → 出现的天数（★ 复发次数比"是否发生"更有信息量）
    degraded_days: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @property
    def title(self) -> str:
        """人类可读标题（`YYYY 年第 NN 周（起 ~ 止）` / `2026-10（起 ~ 止）`）。"""
        if self.period == PERIOD_WEEK:
            iso = date.fromisoformat(self.start).isocalendar()
            return f"{iso[0]} 年第 {iso[1]} 周（{self.start} ~ {self.end}）"
        return f"{self.start[:7]}（{self.start} ~ {self.end}）"

    @property
    def coverage_text(self) -> str:
        """`覆盖 3 / 7 天（缺 ……）`（★ 预拼字符串：jinja 开了 trim_blocks，
        行尾放 `{% endif %}` 会把换行吃掉 —— 2026-10-28 实踩）。"""
        base = f"覆盖 {self.days_covered} / {self.days_expected} 天"
        if not self.missing_days:
            return f"{base}（无缺失）"
        return f"{base}（缺 {'、'.join(self.missing_days)}）"

    @property
    def graded_by_tier_text(self) -> str:
        parts = [f"{key} {value} 篇" for key, value in self.graded_by_tier.items()]
        return f"（{' / '.join(parts)}）" if parts else ""

    @property
    def anomaly_days_text(self) -> str:
        if not self.anomaly_days:
            return "0 天"
        return f"{len(self.anomaly_days)} 天（{'、'.join(self.anomaly_days)}）"

    @property
    def cost_avg_cny(self) -> float | None:
        """日均成本（按**有成本数字**的天算；没样本返回 None，不用 0 冒充）。"""
        if self.cost_days == 0:
            return None
        return round(self.cost_total / self.cost_days, 4)

    @property
    def hit_rate(self) -> float | None:
        """推演命中率（分母 = hit + miss，`insufficient` 不进，与 `stats` 对齐）。"""
        total = self.predictions_hit + self.predictions_miss
        if total == 0:
            return None
        return round(self.predictions_hit / total, 3)


def load_daily_reports(reports_dir: Path) -> tuple[dict[date, DailyReport], list[str]]:
    """读日报 JSON（`reports/daily/` 优先，兼容旧平铺）；★ 坏文件 / 坏 JSON **显式返回原因**。"""
    found: dict[date, DailyReport] = {}
    notes: list[str] = []
    directory = Path(reports_dir)
    if not directory.is_dir():
        return {}, [f"报告目录不存在：{directory}"]
    for day_text, path in iter_daily_jsons(directory):
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            notes.append(f"{path.name}: 日期非法")
            continue
        try:
            report = DailyReport.model_validate_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError) as exc:
            notes.append(f"{path.name}: 解析失败（{type(exc).__name__}）")
            continue
        found[day] = report
    return found, notes


def build_recap(reports_dir: Path, *, window: PeriodWindow, top_n: int = 10) -> RecapReport:
    """聚合窗口内的日报（★ 只读真源，零 LLM）。"""
    daily, notes = load_daily_reports(reports_dir)
    days = window.dates()
    in_window = {d: daily[d] for d in days if d in daily}
    missing = [d.isoformat() for d in days if d not in daily]

    hit_days: dict[str, int] = {}
    hit_titles: dict[str, str] = {}
    hit_groups: dict[str, list[str]] = {}
    graded_by_tier: dict[str, int] = {}
    degraded_days: dict[str, int] = {}
    report = RecapReport(
        period=window.period,
        slug=window.slug,
        start=window.start.isoformat(),
        end=window.end.isoformat(),
        days_covered=len(in_window),
        days_expected=len(days),
        missing_days=missing,
    )

    for report_day in in_window.values():
        report.new_papers += report_day.new_today
        report.runs_total += len(report_day.runs)

        for hit in report_day.watchlist_hits:
            hit_days[hit.id] = hit_days.get(hit.id, 0) + 1
            hit_titles.setdefault(hit.id, hit.title)
            merged = set(hit_groups.get(hit.id, [])) | set(hit.groups)
            hit_groups[hit.id] = sorted(merged)

        report.graded_total += len(report_day.new_graded)
        for graded in report_day.new_graded:
            # ★ tier 为 None = 该日报告没记 Tier（2026-10-28 前的旧报告无此字段）
            key = f"Tier {graded.tier}" if graded.tier is not None else "无 Tier 记录"
            graded_by_tier[key] = graded_by_tier.get(key, 0) + 1
        report.deep_analyzed_total += len(report_day.deep_analyzed)

        if report_day.cost_cny is not None:
            report.cost_total += report_day.cost_cny
            report.cost_days += 1
            if report.cost_peak_cny is None or report_day.cost_cny > report.cost_peak_cny:
                report.cost_peak_cny = report_day.cost_cny
                report.cost_peak_day = report_day.date
        if report_day.cost_anomalies:
            report.anomaly_days.append(report_day.date)

        for pred in report_day.predictions_verified:
            if pred.status == "hit":
                report.predictions_hit += 1
            elif pred.status == "miss":
                report.predictions_miss += 1
            # ★ insufficient / 其他状态不进分母（与 `stats` 同口径）

        for key in report_day.degraded:
            degraded_days[key] = degraded_days.get(key, 0) + 1

    report.cost_total = round(report.cost_total, 4)
    report.graded_by_tier = dict(sorted(graded_by_tier.items()))
    report.degraded_days = dict(sorted(degraded_days.items(), key=lambda kv: (-kv[1], kv[0])))
    report.top_hits = [
        RecapHit(id=paper_id, title=hit_titles[paper_id], days=count, groups=hit_groups[paper_id])
        for paper_id, count in sorted(hit_days.items(), key=lambda kv: (-kv[1], kv[0]))
    ][:top_n]
    if not in_window:
        notes.append(f"窗口内无日报（{window.start} ~ {window.end}）")
    report.notes = notes
    return report


@dataclass
class RecapOutcome:
    recap: RecapReport
    md_path: Path
    json_path: Path


def write_recap(recap: RecapReport, out_dir: Path) -> RecapOutcome:
    """写 `{slug}.md` + `.json`（★ 与 `write_report` 同机制：原子写 + 无生成时间）。"""
    md_path = Path(out_dir) / f"{recap.slug}.md"
    json_path = Path(out_dir) / f"{recap.slug}.json"
    atomic_write(md_path, render_recap(recap))
    atomic_write(json_path, recap.model_dump_json(indent=2) + "\n")
    return RecapOutcome(recap=recap, md_path=md_path, json_path=json_path)
