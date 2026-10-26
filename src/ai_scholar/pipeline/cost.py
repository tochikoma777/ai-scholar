"""L8-F 成本可见性：读历史报告算**日 / 周 / 月 / 年**累计 + 分项 + 异常判定。

★ 真源口径（写死在代码里，别改）：
- **累计以 `data/reports/YYYY-MM-DD.json` 的 `cost_cny` 为准** —— 它是 public 真源、
  跟着 git 走、可审计；db 是"可丢弃缓存"（Actions 用 actions-cache，可能丢）。
- db 的 `llm_usage` 只用来算**分项**（哪一步烧钱），并在输出里标注"缓存，可能不完整"。

★ 为什么需要（2026-10-26 实锤）：首轮 Actions 的 ¥0.2361 里 80% 是废调用，
而报告里 `cost_cny` 一直"有数字" —— 没有累计/分项/环比，异常只能靠人翻日志发现。

★ 本模块**零 LLM、零网络**：只读本地 JSON 与 sqlite。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from statistics import median

from ai_scholar.config import BudgetSettings, Settings

# ★ 与 `llm_usage.purpose` 的实际取值一一对应（grep 得出，勿凭印象改）
USAGE_LABELS = {
    "deep_analysis": "深度分析（strong）",
    "grading": "定级（fast）",
    "enhance": "增强（fast）",
    "enhance_link": "关联解读（fast）",
}


@dataclass(frozen=True)
class CostSummary:
    """一次成本汇总（全部为人民币元）。"""

    today: float = 0.0
    week: float = 0.0
    month: float = 0.0
    year: float = 0.0
    total: float = 0.0
    # 近 7 日（不含今日）中位数：没有样本时为 None（★ 不拿 0 冒充）
    median_7d: float | None = None
    today_delta_pct: float | None = None
    by_purpose: dict[str, float] = field(default_factory=dict)
    anomalies: list[str] = field(default_factory=list)
    report_days: int = 0
    usage_rows: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def has_anomaly(self) -> bool:
        return bool(self.anomalies)


def reports_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "reports"


def load_report_costs(data_dir: Path) -> tuple[dict[date, float], list[str]]:
    """读 `data/reports/*.json` 的 `cost_cny`。返回 `(日期→成本, 坏文件说明)`。

    ★ 坏文件**不静默跳过**：返回说明，由 CLI 展示（静默失败最危险）。
    """
    costs: dict[date, float] = {}
    bad: list[str] = []
    folder = reports_dir(data_dir)
    if not folder.exists():
        return costs, bad
    for path in sorted(folder.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            bad.append(f"{path.name}：JSON 解析失败")
            continue
        raw_day = payload.get("date")
        value = payload.get("cost_cny")
        try:
            day = date.fromisoformat(str(raw_day))
        except (TypeError, ValueError):
            bad.append(f"{path.name}：date 缺失或非法（{raw_day!r}）")
            continue
        if value is None:
            bad.append(f"{path.name}：cost_cny 为 null（该日未记账）")
            continue
        costs[day] = float(value)
    return costs, bad


def _sum_between(costs: dict[date, float], start: date, end: date) -> float:
    return round(sum(v for d, v in costs.items() if start <= d <= end), 4)


def _median_excluding(costs: dict[date, float], *, day: date, days: int) -> float | None:
    start = day - timedelta(days=days)
    values = [v for d, v in costs.items() if start <= d < day]
    if not values:
        return None
    return round(median(values), 4)


def summarize_reports(
    costs: dict[date, float],
    *,
    today: date,
    window_days: int = 7,
    bad_files: list[str] | None = None,
) -> CostSummary:
    """把"日成本"折成滚动窗口/月/年/全部（★ 纯函数，便于单测）。

    `window_days` = 滚动窗口（默认 7 天，含今日）；中位数取**不含今日**的同窗口。
    """
    window_start = today - timedelta(days=max(window_days, 1) - 1)
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    median_7d = _median_excluding(costs, day=today, days=max(window_days, 1))
    today_cost = round(costs.get(today, 0.0), 4)
    delta = None
    if median_7d:
        delta = round((today_cost - median_7d) / median_7d * 100, 1)
    notes = list(bad_files or [])
    if not costs:
        notes.append("没有可用的历史报告（data/reports/*.json）—— 累计只能从 0 开始")
    return CostSummary(
        today=today_cost,
        week=_sum_between(costs, window_start, today),
        month=_sum_between(costs, month_start, today),
        year=_sum_between(costs, year_start, today),
        total=round(sum(costs.values()), 4),
        median_7d=median_7d,
        today_delta_pct=delta,
        report_days=len(costs),
        notes=notes,
    )


def load_usage_by_purpose(
    db_path: Path, *, since: date
) -> tuple[dict[str, float], int, str | None]:
    """本月**分项**成本（来自 db 的 `llm_usage`）。

    返回 `(purpose→金额, 行数, 提示)`。db 不可读 / 表不存在时返回提示而不是抛错
    —— 累计总量不依赖 db，这里只是"锦上添花的拆分"。
    """
    if not db_path.exists():
        return {}, 0, f"db 不存在（{db_path}）—— 分项不可用，累计仍以 reports 为准"
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "select purpose, coalesce(sum(cost_cny), 0), count(*) from llm_usage "
                "where created_at >= ? group by purpose",
                (f"{since.isoformat()}T00:00:00",),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 —— 分项失败不该让 cost 命令挂掉
        return {}, 0, f"读取 llm_usage 失败：{type(exc).__name__}: {exc}"
    by_purpose = {str(p or "unknown"): round(float(total), 4) for p, total, _n in rows}
    return by_purpose, sum(int(n) for *_x, n in rows), None


def detect_anomalies(
    summary: CostSummary, *, budget: BudgetSettings, today: date
) -> list[str]:
    """异常判定（规则必须可解释，见任务卡 §3）。

    ★ 只**标注**不阻断：阻断是 `check_budget` 的职责，两者别混。
    """
    reasons: list[str] = []
    half_day = round(float(budget.daily_limit_cny) * 0.5, 4)
    if summary.today > half_day:
        reasons.append(
            f"今日 ¥{summary.today:.4f} > 日限额一半 ¥{half_day:.2f}"
            f"（{budget.daily_limit_cny:.2f}/天）"
        )
    if summary.median_7d and summary.today > summary.median_7d * 3:
        reasons.append(
            f"今日 ¥{summary.today:.4f} > 近 7 日中位数 ¥{summary.median_7d:.4f} × 3"
            "（相对突增）"
        )
    month_cap = round(float(budget.monthly_limit_cny) * 0.8, 4)
    if summary.month > month_cap:
        reasons.append(
            f"本月 ¥{summary.month:.4f} > 月限额 80% ¥{month_cap:.2f}"
            f"（上限 {budget.monthly_limit_cny:.2f}）"
        )
    return reasons


def live_anomalies(
    *,
    data_dir: Path,
    today: date,
    today_cost: float,
    budget: BudgetSettings,
    window_days: int = 7,
) -> list[str]:
    """报告生成时的**实时**异常判定（今日成本由调用方给，历史读 reports）。

    ★ 为什么不直接用 `build_summary`：报告此刻还没写盘，今日的成本只存在于 db，
      不能等 `reports/今天.json` 出现；同时要避免"今天的旧报告 + 今日实时值"重复计数。
    """
    costs, _bad = load_report_costs(data_dir)
    month_start = today.replace(day=1)
    month = round(
        sum(v for d, v in costs.items() if month_start <= d < today) + float(today_cost), 4
    )
    probe = CostSummary(
        today=round(float(today_cost), 4),
        month=month,
        median_7d=_median_excluding(costs, day=today, days=window_days),
    )
    return detect_anomalies(probe, budget=budget, today=today)


def build_summary(
    *,
    settings: Settings,
    today: date | None = None,
    db_path: Path | None = None,
    window_days: int = 7,
) -> CostSummary:
    """读真源 → 汇总 → 分项 → 异常（CLI 与将来的报告 guard 共用）。"""
    from ai_scholar.utils.timeutil import today_local

    day = today or today_local()
    costs, bad = load_report_costs(settings.data_dir())
    base = summarize_reports(costs, today=day, window_days=window_days, bad_files=bad)
    by_purpose, rows, note = load_usage_by_purpose(
        Path(db_path) if db_path else settings.db_path(), since=day.replace(day=1)
    )
    notes = list(base.notes)
    if note:
        notes.append(note)
    summary = CostSummary(
        today=base.today,
        week=base.week,
        month=base.month,
        year=base.year,
        total=base.total,
        median_7d=base.median_7d,
        today_delta_pct=base.today_delta_pct,
        by_purpose=by_purpose,
        anomalies=detect_anomalies(base, budget=settings.budget, today=day),
        report_days=base.report_days,
        usage_rows=rows,
        notes=notes,
    )
    return summary


def to_json(summary: CostSummary, *, today: date) -> dict[str, object]:
    """`--json` 输出（字段名稳定，供脚本/将来的告警用）。"""
    return {
        "date": today.isoformat(),
        "today": summary.today,
        "week": summary.week,
        "month": summary.month,
        "year": summary.year,
        "total": summary.total,
        "median_7d": summary.median_7d,
        "today_delta_pct": summary.today_delta_pct,
        "by_purpose": summary.by_purpose,
        "by_purpose_source": "db.llm_usage（缓存，可能不完整）",
        "anomalies": summary.anomalies,
        "anomaly": summary.has_anomaly,
        "report_days": summary.report_days,
        "usage_rows": summary.usage_rows,
        "notes": summary.notes,
    }
