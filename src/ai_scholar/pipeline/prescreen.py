"""零成本预筛（规则负面排除，★ 不走 LLM）。

★ 只决定「是否进 pending（是否值得跟踪）」，**绝不决定是否入库** ——
  `papers` 表照收全量。watchlist 已经因为同类层次混淆返工过两次，
  预筛不能成为第三次（执行手册红线 15 / 设计方案 §4.2.1）。
★ `retain_ratio = 0.2` 是**淘汰集抽样留存率**，不是总体放行率：
  总量 = `N×p + N×(1-p)×r`。

抽样用 `sha256(paper_id)` 的确定性取模，而不是 `random`：
① 同一篇论文永远得到同一结论（可复现、可回看） ② 单测不需要打桩随机数。
"""

from __future__ import annotations

import hashlib
from datetime import date

from ai_scholar.config import PrescreenSettings, SignalsSettings
from ai_scholar.store.models import (
    SIGNAL_PENDING,
    SIGNAL_READY,
    SIGNAL_REJECTED,
    SIGNAL_SAMPLE,
    Paper,
)
from ai_scholar.utils.timeutil import age_days_since


def prescreen(paper: Paper, cfg: PrescreenSettings) -> tuple[bool, str | None]:
    """返回 `(是否通过, 淘汰原因)`。★ 这是规则负面排除，不涉及任何模型调用。"""
    if not (paper.title or "").strip():
        return False, "缺少标题"

    abstract = (paper.abstract or "").strip()
    if len(abstract) < cfg.min_abstract_chars:
        return False, f"摘要过短（{len(abstract)} < {cfg.min_abstract_chars} 字符），无法评估"

    lowered = paper.title.lower()
    for pattern in cfg.exclude_title_patterns:
        if pattern.lower() in lowered:
            return False, f"标题命中排除词：{pattern}"
    return True, None


def retention_score(paper_id: str) -> int:
    """`0..9999` 的确定性分数（用于淘汰集抽样，可复现）。"""
    return int(hashlib.sha256(paper_id.encode("utf-8")).hexdigest()[:8], 16) % 10000


def should_retain(paper_id: str, ratio: float) -> bool:
    """淘汰集是否进入留样集（★ ratio 是**淘汰集**的抽样留存率）。"""
    return retention_score(paper_id) < int(ratio * 10000)


def apply_prescreen(
    paper: Paper,
    *,
    prescreen_cfg: PrescreenSettings,
    signals_cfg: SignalsSettings,
    today: date,
) -> bool:
    """跑一次预筛并落状态。返回是否通过（★ 通过与否不影响论文是否在库里）。

    - 通过：`pending`（或快路径 `ready`：published_at ≥ 180 天，跳过 pending 等 L3 定级）
    - 淘汰：按 r 抽留样 ⇒ `sample`；未抽中 ⇒ `rejected`
    """
    passed, reason = prescreen(paper, prescreen_cfg)
    paper.prescreen_reason = reason

    if passed:
        age = age_days_since(paper.published_at, today)
        if age is not None and age >= signals_cfg.maturity_days:
            paper.signal_state = SIGNAL_READY
            # 快路径：成熟论文跳过 pending，等 L3 直接定级（不做常规采样）
            paper.signal_next_at = None
        else:
            paper.signal_state = SIGNAL_PENDING
            paper.signal_next_at = today.isoformat()
        return True

    if should_retain(paper.id, prescreen_cfg.retain_ratio):
        paper.signal_state = SIGNAL_SAMPLE
        paper.signal_next_at = today.isoformat()
    else:
        paper.signal_state = SIGNAL_REJECTED
        paper.signal_next_at = None
    return False
