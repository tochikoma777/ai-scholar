"""DeepSeek 高峰时段守卫（零网络、零 LLM、纯函数）。

★ 为什么需要：官方 2026-08-17 起分时定价 —— 高峰 = **北京时间周一至周五
  9:00–12:00 / 14:00–18:00**（= UTC 01:00–04:00 / 06:00–10:00），其余（含周末、
  中国法定节假日）为空闲；**空闲价 = 高峰价的一半**。
  当前 `daily.yml` cron 是 UTC 16:30（北京次日 00:30，已在空闲），但 GitHub 排队
  延迟实测 4–8 小时，曾把 LLM 调用拖到 UTC 00:22（北京 08:22，距高峰仅 38 分钟）。
  ⇒ 在 LLM 步骤前判一次：落进高峰就把 `AI_SCHOLAR_MODE=off` 写进 `$GITHUB_ENV`，
  让 enhance / grade / analyze 零调用（三处都有 `RunMode.OFF` 早退），
  其余零 LLM 步骤照跑；次日空闲自动补跑（周配额 5 篇，不丢条目）。

★ 保守策略：忽略中国法定节假日 —— 把节假日也当高峰，只会「多降级」，
  不会「漏进高峰」；成本优先时这是正确方向。

★ 窗口前后各留 `MARGIN_MIN` 分钟余量：guard 跑完到真正发起 LLM 调用还有
  若干分钟（Migrate/Collect 等步骤），留余量避免「判时空闲、调用时已进高峰」。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from ai_scholar.utils.timeutil import utcnow

# UTC 分钟窗口：(start, end)，对应北京工作日 9:00-12:00 / 14:00-18:00
PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((60, 240), (360, 600))
MARGIN_MIN = 15


def is_peak(now: datetime) -> bool:
    """`now` 是否落在 DeepSeek 高峰窗口（含前后余量）。

    `now` 必须是 aware datetime；内部按 UTC 判断。周六/周日全天空闲。
    """
    utc = now.astimezone(UTC)
    if utc.weekday() >= 5:  # 5=周六, 6=周日
        return False
    minutes = utc.hour * 60 + utc.minute
    return any(start - MARGIN_MIN <= minutes < end + MARGIN_MIN for start, end in PEAK_WINDOWS_UTC)


def main(now: datetime | None = None) -> int:
    """GitHub Actions 入口：高峰时降级 off；其余情况不动配置。"""
    now = now or utcnow()
    if not is_peak(now):
        print(f"peak-guard: off-peak at {now:%Y-%m-%d %H:%M}Z — LLM enabled")
        return 0

    print(
        f"::warning title=peak-guard::UTC {now:%H:%M} 落在 DeepSeek 高峰窗口"
        "（北京工作日 9-12/14-18），降级 AI_SCHOLAR_MODE=off（本日 LLM 零调用）"
    )
    github_env = os.getenv("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as handle:
            handle.write("AI_SCHOLAR_MODE=off\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
