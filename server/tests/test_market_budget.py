"""行情预算与冷却分级测试。

两条要点：

1. **被拒的尝试不消耗额度**——否则限流会自我加剧；
2. **证券级失败不冷却整个来源**——一个坏符号不该让同来源的其他符号一起失败。
"""

from __future__ import annotations

from lyra.application.market_budget import (
    DEFAULT_COOLDOWN_MS,
    MINUTE_MS,
    PER_INSTRUMENT_ISSUES,
    MarketProviderPolicy,
    ProviderBudget,
    cooldown_ms,
    should_block_provider,
)

NOW = 1_760_000_000_000
POLICY = MarketProviderPolicy(
    cache_ttl_ms=30_000, max_requests_per_minute=3, max_requests_per_day=10
)
UNLIMITED_DAILY = MarketProviderPolicy(
    cache_ttl_ms=30_000, max_requests_per_minute=3, max_requests_per_day=None
)


# --------------------------------------------------------------------------
# 滚动窗口
# --------------------------------------------------------------------------


def test_allows_calls_up_to_the_minute_limit() -> None:
    """分钟额度内的调用放行。"""
    budget = ProviderBudget()

    assert [budget.consume(POLICY, NOW) for _ in range(3)] == [None, None, None]
    # 第四次被拒。
    assert budget.consume(POLICY, NOW) == "rate_limited"


def test_rejected_attempts_do_not_consume_budget() -> None:
    """**被拒的尝试不消耗额度。**

    否则限流会自我加剧：每次被拒也占一个名额，窗口越挤越紧。
    """
    budget = ProviderBudget()
    for _ in range(3):
        budget.consume(POLICY, NOW)
    before = len(budget.minute_calls)

    budget.consume(POLICY, NOW)

    assert len(budget.minute_calls) == before


def test_minute_window_slides() -> None:
    """窗口滑过后额度恢复。"""
    budget = ProviderBudget()
    for _ in range(3):
        budget.consume(POLICY, NOW)

    assert budget.consume(POLICY, NOW + MINUTE_MS) is None


def test_daily_limit_applies_across_minutes() -> None:
    """每日额度跨分钟累计。"""
    budget = ProviderBudget()
    # 每分钟 3 次，3 分钟后用掉 9 次，第 10 次是最后一次。
    for index in range(10):
        assert budget.consume(POLICY, NOW + index * MINUTE_MS) is None, index
    assert budget.consume(POLICY, NOW + 10 * MINUTE_MS) == "rate_limited"


def test_daily_window_slides() -> None:
    """每日窗口滑过后额度恢复。"""
    budget = ProviderBudget()
    for index in range(10):
        budget.consume(POLICY, NOW + index * MINUTE_MS)

    assert budget.consume(POLICY, NOW + 86_400_000) is None


def test_no_daily_limit_means_no_daily_tracking() -> None:
    """每日额度为 ``None`` 时不记录每日调用——省掉一份永远不会被读的列表。"""
    budget = ProviderBudget()
    for index in range(5):
        budget.consume(UNLIMITED_DAILY, NOW + index * MINUTE_MS)

    assert budget.daily_calls == []


# --------------------------------------------------------------------------
# 冷却
# --------------------------------------------------------------------------


def test_blocked_provider_returns_the_blocked_reason() -> None:
    """冷却期内返回冷却原因，而不是继续尝试。"""
    budget = ProviderBudget()
    budget.block(NOW + 5 * MINUTE_MS, "credentials_rejected")

    assert budget.consume(POLICY, NOW) == "credentials_rejected"


def test_block_takes_the_later_deadline() -> None:
    """重复冷却取更晚的时刻——不能让一次较短的冷却缩短已有的较长冷却。"""
    budget = ProviderBudget()
    budget.block(NOW + 10 * MINUTE_MS, "rate_limited")
    budget.block(NOW + 60_000, "provider_unavailable")

    assert budget.blocked_until == NOW + 10 * MINUTE_MS
    assert budget.blocked_reason == "provider_unavailable"


def test_cooldown_is_graded_by_reason() -> None:
    """冷却时长按原因分级。"""
    assert cooldown_ms("rate_limited") == 15 * MINUTE_MS
    assert cooldown_ms("credentials_rejected") == 5 * MINUTE_MS
    assert cooldown_ms("no_data") == 15 * MINUTE_MS
    assert cooldown_ms("provider_unavailable") == DEFAULT_COOLDOWN_MS
    assert cooldown_ms("timeout") == DEFAULT_COOLDOWN_MS


# --------------------------------------------------------------------------
# 证券级 vs 来源级
# --------------------------------------------------------------------------


def test_per_instrument_issues_do_not_block_the_provider() -> None:
    """**证券级失败不冷却整个来源。**

    一个坏符号只说明这个符号有问题；冷却来源会让同来源的其他符号一起失败。
    """
    for reason in PER_INSTRUMENT_ISSUES:
        assert should_block_provider(reason) is False, reason


def test_provider_level_issues_do_block() -> None:
    """来源级失败才冷却来源。"""
    for reason in ("rate_limited", "credentials_rejected", "provider_unavailable", "timeout"):
        assert should_block_provider(reason) is True, reason


def test_per_instrument_set_is_exactly_the_documented_four() -> None:
    """清单是闭集：新增原因时必须显式决定它属于哪一级。"""
    assert set(PER_INSTRUMENT_ISSUES) == {
        "no_data", "unsupported_instrument", "currency_unsupported", "invalid_response"
    }
