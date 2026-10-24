"""预算闸门测试（阶段 0：预估 + 四层上限 + 单次 run 次数，纯逻辑，零网络）。

★ 断言「实际值」：预估数字必须对上成本预算 §2 的成本模型，不只看"能算出一个数"。
"""

from __future__ import annotations

import pytest

from ai_scholar.budget import BudgetState, check_budget, estimate_cost
from ai_scholar.config import BudgetSettings, load_settings
from ai_scholar.utils.errors import ConfigError


def _budget() -> BudgetSettings:
    """读仓库默认配置（价格表已落在 config/default.yaml）。"""
    return load_settings().budget


def test_flash_estimate_matches_cost_model() -> None:
    """每日报告（flash）：30k in / 4k out → ¥0.038（成本预算 §2）。"""
    est = estimate_cost(_budget(), "deepseek", "deepseek-v4-flash", 30000, 4000)
    assert est.cost_cny == pytest.approx(0.038, abs=1e-9)


def test_pro_estimate_matches_cost_model() -> None:
    """深度分析（pro）：60k in / 8k out → ¥0.228（成本预算 §2）。"""
    est = estimate_cost(_budget(), "deepseek", "deepseek-v4-pro", 60000, 8000)
    assert est.cost_cny == pytest.approx(0.228, abs=1e-9)


def test_cached_input_is_cheaper_than_miss() -> None:
    """★ 三档单价：缓存命中 0.02/M 远低于未命中 1.0/M，预估默认按未命中（保守）。"""
    b = _budget()
    miss = estimate_cost(b, "deepseek", "deepseek-v4-flash", 1_000_000, 0)
    hit = estimate_cost(b, "deepseek", "deepseek-v4-flash", 1_000_000, 0, input_cached=True)
    assert miss.cost_cny == pytest.approx(1.0)
    assert hit.cost_cny == pytest.approx(0.02)
    assert hit.cost_cny < miss.cost_cny


def test_output_price_is_charged() -> None:
    """输出 token 按 max_tokens × 输出单价计（pro 输出 6.0/M）。"""
    b = _budget()
    est = estimate_cost(b, "deepseek", "deepseek-v4-pro", 0, 1_000_000)
    assert est.cost_cny == pytest.approx(6.0)


def test_price_for_missing_key_raises() -> None:
    """价格表按 (provider, model) 键控：缺键必须显式报错，不能静默算出 0。"""
    with pytest.raises(ConfigError):
        _budget().price_for("openai", "gpt-99")


def test_repo_default_prices_loaded() -> None:
    """仓库默认价格表的三档单价必须与成本预算 §1 一致（断言实际值）。"""
    b = _budget()
    flash = b.price_for("deepseek", "deepseek-v4-flash")
    pro = b.price_for("deepseek", "deepseek-v4-pro")
    assert (
        flash.input_cached_cny_per_1m,
        flash.input_miss_cny_per_1m,
        flash.output_cny_per_1m,
    ) == (0.02, 1.0, 2.0)
    assert (
        pro.input_cached_cny_per_1m,
        pro.input_miss_cny_per_1m,
        pro.output_cny_per_1m,
    ) == (0.025, 3.0, 6.0)


def test_repo_default_limits() -> None:
    """四层上限默认值 = 成本预算 §4（日 2 / 周 10 / 月 40 / 年 200）。"""
    b = _budget()
    assert (b.daily_limit_cny, b.weekly_limit_cny, b.monthly_limit_cny, b.yearly_limit_cny) == (
        2.0,
        10.0,
        40.0,
        200.0,
    )


def test_check_budget_allows_within_limits() -> None:
    b = _budget()
    est = estimate_cost(b, "deepseek", "deepseek-v4-flash", 30000, 4000)
    assert check_budget(b, BudgetState(), est).allowed


def test_check_budget_blocks_daily_overflow() -> None:
    b = _budget()
    est = estimate_cost(b, "deepseek", "deepseek-v4-flash", 30000, 4000)
    decision = check_budget(b, BudgetState(spent_daily=b.daily_limit_cny), est)
    assert not decision.allowed
    assert "日额度超限" in decision.reason


def test_check_budget_blocks_weekly_overflow() -> None:
    b = _budget()
    est = estimate_cost(b, "deepseek", "deepseek-v4-flash", 30000, 4000)
    decision = check_budget(b, BudgetState(spent_weekly=b.weekly_limit_cny), est)
    assert not decision.allowed
    assert "周额度超限" in decision.reason


def test_check_budget_blocks_monthly_overflow() -> None:
    b = _budget()
    est = estimate_cost(b, "deepseek", "deepseek-v4-flash", 30000, 4000)
    decision = check_budget(b, BudgetState(spent_monthly=b.monthly_limit_cny), est)
    assert not decision.allowed
    assert "月额度超限" in decision.reason


def test_check_budget_blocks_yearly_overflow() -> None:
    b = _budget()
    est = estimate_cost(b, "deepseek", "deepseek-v4-flash", 30000, 4000)
    decision = check_budget(b, BudgetState(spent_yearly=b.yearly_limit_cny), est)
    assert not decision.allowed
    assert "年额度超限" in decision.reason


def test_check_budget_blocks_call_count() -> None:
    b = _budget()
    est = estimate_cost(b, "deepseek", "deepseek-v4-flash", 30000, 4000)
    decision = check_budget(b, BudgetState(calls_this_run=b.max_calls_per_run), est)
    assert not decision.allowed
    assert "调用次数" in decision.reason
