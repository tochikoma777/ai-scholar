"""时间工具。

铁律：代码内一律使用 **UTC**（不带时区的"取当前时间"调用被禁用），
只有渲染层才转成 `Asia/Shanghai` 展示。

★ 三态时间语义（本项目特别重要）：
- `event`：论文自己声明的发表/更新时间（arXiv `published` / `updated`）；
- `state`：被观测时的状态（HF 上榜票数、被引数）；
- `discovery`：**我们第一次见到它的日期**（`first_seen_at`）。

arXiv 与 HF 的索引相对真实发表滞后数天，所以"今天新发现了什么"必须用
`discovery` 过滤，用 `published_at` 过滤会把滞后几天才入索引的论文漏掉。
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

UTC = UTC
DEFAULT_TZ = "Asia/Shanghai"

_DURATION_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[smhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def utcnow() -> datetime:
    """当前 UTC 时间（带 tzinfo）。代码内取"现在"只能用这个函数。"""
    return datetime.now(tz=UTC)


def tz(name: str = DEFAULT_TZ) -> ZoneInfo:
    """取时区对象。"""
    return ZoneInfo(name)


def to_local(dt: datetime, name: str = DEFAULT_TZ) -> datetime:
    """UTC（或任意带时区的时间）转本地时区。naive 输入按 UTC 解释。"""
    return ensure_utc(dt).astimezone(tz(name))


def now_local(name: str = DEFAULT_TZ) -> datetime:
    """当前本地时间（渲染层用）。"""
    return utcnow().astimezone(tz(name))


def today_local(name: str = DEFAULT_TZ) -> date:
    """本地时区的"今天"（用于报告日期与 HF daily_papers 的 date 参数）。"""
    return now_local(name).date()


def ensure_utc(dt: datetime) -> datetime:
    """naive 时间按 UTC 解释；aware 时间统一转成 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_local(dt: datetime, name: str = DEFAULT_TZ, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """格式化为本地时间字符串（渲染层用）。"""
    return to_local(dt, name).strftime(fmt)


def parse_duration(text: str | timedelta) -> timedelta:
    """解析时长字符串：`30s` / `30m` / `24h` / `7d` / `2w`；也可直接传 timedelta。"""
    if isinstance(text, timedelta):
        return text
    match = _DURATION_RE.match(text.strip())
    if not match:
        raise ValueError(f"无法解析时长：{text!r}（示例：30m / 24h / 7d）")
    seconds = float(match.group("value")) * _UNIT_SECONDS[match.group("unit").lower()]
    return timedelta(seconds=seconds)


def window_since(
    since: str | timedelta, until: datetime | None = None
) -> tuple[datetime, datetime]:
    """由 `--since 24h` 这类参数算出 `[since, until)` 的 UTC 时间窗。"""
    end = ensure_utc(until) if until is not None else utcnow()
    return end - parse_duration(since), end


def last_hours(hours: float, until: datetime | None = None) -> tuple[datetime, datetime]:
    """最近 N 小时的 UTC 时间窗。"""
    end = ensure_utc(until) if until is not None else utcnow()
    return end - timedelta(hours=hours), end


def day_window(day: date, name: str = DEFAULT_TZ) -> tuple[datetime, datetime]:
    """某**本地日**对应的 UTC 半开区间 `[start, end)`。

    报告按"本地日"归档，但库里存 UTC，所以必须由本地 00:00 反推 UTC 边界——
    直接按 UTC 切日会把北京时间的凌晨 0-8 点算进前一天。
    """
    zone = tz(name)
    start_local = datetime(day.year, day.month, day.day, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def age_days(published_at: datetime, now: datetime | None = None) -> float:
    """内容"年龄"（天）。用于「历史地位」条件式输出里的"发表 N 天，被引 M 次"。"""
    ref = ensure_utc(now) if now is not None else utcnow()
    return max(0.0, (ref - ensure_utc(published_at)).total_seconds() / 86400.0)
