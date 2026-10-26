"""LLM 调用共享辅助：预算窗口汇总 + 实际成本。

★ 纯 DB 查询 / 纯计算，不发起网络；`enhance` / `grade` / `deep_analysis` 共用。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from sqlmodel import Session, select

from ai_scholar.budget import BudgetState, estimate_cost
from ai_scholar.config import Settings
from ai_scholar.llm import LLMResponse
from ai_scholar.store.models import LLMUsage, Paper, PaperReference
from ai_scholar.utils.timeutil import tz

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_ENV = Environment(loader=FileSystemLoader(str(_PROMPT_DIR)), autoescape=False)


def render_prompt(name: str, **kwargs: Any) -> str:
    return _ENV.get_template(name).render(**kwargs)


def estimate_input_tokens(text: str) -> int:
    """调用前的输入 token 粗估（保守：英中混合按 3 字符/token）。"""
    return max(1, len(text) // 3)


def validate_output(
    content: str,
    *,
    tokens_out: int | None = None,
    max_tokens: int | None = None,
    min_chars: int = 0,
    skeleton: tuple[str, ...] = (),
    skeleton_name: str = "骨架",
) -> tuple[bool, str]:
    """★ 通用输出判废（E8）：空 / 过短 / 缺骨架 / **`tokens_out` 顶格**。

    顶格 = 推理把额度吃光，答案即使"看起来有结构"也是残稿。

    ★ 断言打在**真实结构**上，**不打在「模型是否听话」上**（如"必须回显 paper_id"）——
    真实模型经常不回显，那条判据会把"截断"误诊成"不听话"（2026-10-26 Actions 实锤）。

    Args:
        min_chars: 最小字符数（0 = 不检查长度）。
        skeleton: 至少命中一个的骨架标记（空 = 不检查结构）。
    """
    top_grid = tokens_out is not None and max_tokens and tokens_out >= max_tokens - 1
    if not content.strip():
        if top_grid:
            return False, f"推理占满全部额度，答案为空（tokens_out={tokens_out}/{max_tokens}）"
        return False, "输出为空"
    if min_chars and len(content) < min_chars:
        return False, f"输出过短（<{min_chars} 字符）"
    if skeleton and not any(marker in content for marker in skeleton):
        return False, f"缺少 {skeleton_name}"
    if top_grid:
        return False, f"输出顶格疑似截断（tokens_out={tokens_out}/{max_tokens}）"
    return True, ""


def authors_text(paper: Paper) -> str:
    return ", ".join(json.loads(paper.authors_json or "[]"))


def year_text(paper: Paper) -> str:
    return (paper.published_at or "")[:4] or "—"


def references_text(session: Session, paper_id: str, *, limit: int = 30) -> str:
    """参考文献外部 ID（定级/深度分析 prompt 用）。"""
    rows = session.exec(select(PaperReference).where(PaperReference.paper_id == paper_id)).all()
    refs = [row.ref_external_id for row in rows][:limit]
    return ", ".join(refs) if refs else "（暂无）"


def window_starts(day: date) -> dict[str, datetime]:
    """日/周/月/年窗口起点（本地 Asia/Shanghai 起算 → UTC）。"""
    zone = tz()
    day_start = datetime.combine(day, time.min, tzinfo=zone)
    week_start = datetime.combine(day - timedelta(days=day.weekday()), time.min, tzinfo=zone)
    month_start = datetime.combine(day.replace(day=1), time.min, tzinfo=zone)
    year_start = datetime.combine(day.replace(month=1, day=1), time.min, tzinfo=zone)
    return {
        "daily": day_start.astimezone(UTC),
        "weekly": week_start.astimezone(UTC),
        "monthly": month_start.astimezone(UTC),
        "yearly": year_start.astimezone(UTC),
    }


def spent_in_windows(session: Session, *, today: date, calls_this_run: int = 0) -> BudgetState:
    """从 `llm_usage` 汇总各窗口已花费（成本预算 §3.4 记账闭环）。"""
    starts = window_starts(today)
    spent = {"daily": 0.0, "weekly": 0.0, "monthly": 0.0, "yearly": 0.0}
    for row in session.exec(select(LLMUsage)).all():
        if row.cost_cny is None or not row.created_at:
            continue
        for key, start in starts.items():
            if row.created_at >= start.isoformat(timespec="seconds"):
                spent[key] += row.cost_cny
    return BudgetState(
        spent_daily=spent["daily"],
        spent_weekly=spent["weekly"],
        spent_monthly=spent["monthly"],
        spent_yearly=spent["yearly"],
        calls_this_run=calls_this_run,
    )


def actual_cost(settings: Settings, provider: str, model: str, response: LLMResponse) -> float:
    """调用后按**实际 usage** 算成本（把实际输出 token 传进 max_tokens 位）。"""
    return estimate_cost(
        settings.budget,
        provider,
        model,
        response.tokens_in,
        response.tokens_out,
        input_cached=response.cache_hit,
    ).cost_cny
