"""成本闸门（成本预算 §3，硬约束）。

★ 原则：预算决定「做多少篇」，不决定「单篇做多浅」。
  钱不够 = 少做或进队列，**绝不截断单次生成**。

- 闸门在**调用前**：每次 LLM 调用先预估成本，累计到任一额度即不再发起新调用。
- 预估公式：`输入 token × 输入单价 + max_tokens × 输出单价`（元/百万 token）。
  ★ 调用前无法知道缓存命中与否 ⇒ 默认按**未命中**（最贵）计，保守。
- 四层上限（日/周/月/年）+ 单次 run 调用次数，全部可配置；超限**硬停、退出码 0**。

本模块只做纯逻辑，**不读写数据库、不发网络**：
LLM 调用层（`enhance` / `grade` / `deep_analysis`）在发起调用前调用
:func:`estimate_cost` + :func:`check_budget`；「已累计」状态由
:func:`ai_scholar.pipeline.llm_support.spent_in_windows` 从 `llm_usage` 表查询后传入。

★ 记账闭环**已完成**（原「L3 待办」三条均已落地，勿重复施工）：
1. `llm_usage` 已有 `cost_cny` + `cache_hit`（alembic 迁移 `f1c8b4d9e2a7`）；
2. 每次调用后三处调用点都会写 `llm_usage`（**含解析失败的调用** —— 钱已花）；
3. `spent_in_windows` 按日/周/月/年窗口汇总 + 本次 run 调用次数。
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_scholar.config import BudgetSettings


@dataclass(frozen=True)
class CostEstimate:
    """一次调用的成本预估结果。"""

    provider: str
    model: str
    input_tokens: int
    max_tokens: int
    cost_cny: float
    input_cached: bool = False


@dataclass(frozen=True)
class BudgetState:
    """闸门需要的「已累计」状态。

    由 `llm_support.spent_in_windows` 从 `llm_usage` 按时间窗口 / 本次 run 查询后传入；
    `budget` 命令用命令行参数模拟（离线演练）。
    """

    spent_daily: float = 0.0
    spent_weekly: float = 0.0
    spent_monthly: float = 0.0
    spent_yearly: float = 0.0
    calls_this_run: int = 0


@dataclass(frozen=True)
class BudgetDecision:
    """闸门判定：放行 or 拦截（带原因，绝不截断生成）。"""

    allowed: bool
    reason: str = ""
    cost_cny: float = 0.0


def estimate_cost(
    settings: BudgetSettings,
    provider: str,
    model: str,
    input_tokens: int,
    max_tokens: int,
    *,
    input_cached: bool = False,
) -> CostEstimate:
    """预估一次调用的成本（元）。

    ★ 调用前不知道缓存命中 ⇒ 默认按未命中计价（保守）；实际命中与否记账时才回填。
    """
    price = settings.price_for(provider, model)
    in_price = price.input_cached_cny_per_1m if input_cached else price.input_miss_cny_per_1m
    cost = input_tokens / 1_000_000 * in_price + max_tokens / 1_000_000 * price.output_cny_per_1m
    return CostEstimate(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        max_tokens=max_tokens,
        cost_cny=cost,
        input_cached=input_cached,
    )


def check_budget(
    settings: BudgetSettings,
    state: BudgetState,
    estimate: CostEstimate,
) -> BudgetDecision:
    """调用前闸门：单次 run 调用次数 + 四层上限，任一超限 ⇒ 拦截。

    ★ 拦截 = 本次调用**不发起**（任务进队列），不是截断生成。
    """
    if state.calls_this_run + 1 > settings.max_calls_per_run:
        return BudgetDecision(
            allowed=False,
            reason=f"单次 run 调用次数已达上限 {settings.max_calls_per_run}",
            cost_cny=estimate.cost_cny,
        )

    limits = (
        ("日", state.spent_daily, settings.daily_limit_cny),
        ("周", state.spent_weekly, settings.weekly_limit_cny),
        ("月", state.spent_monthly, settings.monthly_limit_cny),
        ("年", state.spent_yearly, settings.yearly_limit_cny),
    )
    for label, spent, limit in limits:
        if spent + estimate.cost_cny > limit:
            reason = (
                f"{label}额度超限：已用 ¥{spent:.4f} + 本次 ¥{estimate.cost_cny:.4f} > ¥{limit}"
            )
            return BudgetDecision(
                allowed=False,
                reason=reason,
                cost_cny=estimate.cost_cny,
            )
    return BudgetDecision(allowed=True, cost_cny=estimate.cost_cny)
