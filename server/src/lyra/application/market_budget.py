"""行情来源的请求预算与冷却分级。

抽自 TypeScript 版 ``market-data-service.ts``。

**预算不是"供应商承诺的额度"**，而是 Lyra 自己的保守上限：滚动 1 分钟与滚动 24 小时。
超出时返回 ``rate_limited`` 而**不记录**这次调用——否则一次被拒的尝试也会吃掉额度，
让限流自我加剧。

冷却时长按原因分级，其中一条尤其重要：**证券级失败不冷却整个来源**。

| 原因 | 冷却 |
| --- | --- |
| `rate_limited` | 15 分钟 |
| `credentials_rejected` | 5 分钟 |
| `no_data` / `unsupported_instrument` | 15 分钟 |
| 其它（传输/来源不可用等） | 60 秒 |
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.market_data.types import MarketQuoteIssue

MINUTE_MS = 60_000
DAY_MS = 86_400_000

# 只影响**单个证券**的失败状态。
#
# 这些状态来自单证券的数据缺失、标的不支持或报价形状校验，必须只短期缓存该证券；
# 若拿去冷却整个来源，**一个坏符号会连带让同来源的其他符号在冷却期内全部失败**。
PER_INSTRUMENT_ISSUES: tuple[MarketQuoteIssue, ...] = (
    "no_data", "unsupported_instrument", "currency_unsupported", "invalid_response",
)

_COOLDOWN_MS: dict[str, int] = {
    "rate_limited": 15 * MINUTE_MS,
    "credentials_rejected": 5 * MINUTE_MS,
    "no_data": 15 * MINUTE_MS,
    "unsupported_instrument": 15 * MINUTE_MS,
}
DEFAULT_COOLDOWN_MS = MINUTE_MS


@dataclass(frozen=True)
class MarketProviderPolicy:
    """单个来源的缓存与预算策略。"""

    cache_ttl_ms: int
    max_requests_per_minute: int
    # 保守的滚动 24 小时预算，不是对供应额度的承诺；``None`` 表示不限制。
    max_requests_per_day: int | None


@dataclass
class ProviderBudget:
    """一个来源的滚动窗口预算与冷却状态。"""

    minute_calls: list[int] = field(default_factory=list)
    daily_calls: list[int] = field(default_factory=list)
    blocked_until: int = 0
    blocked_reason: MarketQuoteIssue = "provider_unavailable"

    def consume(self, policy: MarketProviderPolicy, timestamp_ms: int) -> MarketQuoteIssue | None:
        """尝试消费一次额度。

        @param policy: 该来源的策略。
        @param timestamp_ms: 当前时间。
        @returns ``None`` 表示放行；否则给出拒绝原因。
        """
        if self.blocked_until > timestamp_ms:
            return self.blocked_reason

        self.minute_calls = [
            called for called in self.minute_calls if timestamp_ms - called < MINUTE_MS
        ]
        self.daily_calls = [
            called for called in self.daily_calls if timestamp_ms - called < DAY_MS
        ]
        if len(self.minute_calls) >= policy.max_requests_per_minute or (
            policy.max_requests_per_day is not None
            and len(self.daily_calls) >= policy.max_requests_per_day
        ):
            # **不记录被拒的尝试**：否则限流会自我加剧。
            return "rate_limited"

        self.minute_calls.append(timestamp_ms)
        if policy.max_requests_per_day is not None:
            # 不限制每日额度时也不记录——省掉一份永远不会被读的列表。
            self.daily_calls.append(timestamp_ms)
        return None

    def block(self, until_ms: int, reason: MarketQuoteIssue) -> None:
        """把一个来源冷却到指定时刻。

        **只用于来源级失败**；证券级失败（见 :data:`PER_INSTRUMENT_ISSUES`）不得调用它。
        """
        self.blocked_until = max(self.blocked_until, until_ms)
        self.blocked_reason = reason


def cooldown_ms(reason: MarketQuoteIssue) -> int:
    """按原因返回冷却时长。

    @param reason: 失败原因。
    @returns 冷却毫秒数。
    """
    return _COOLDOWN_MS.get(reason, DEFAULT_COOLDOWN_MS)


def should_block_provider(reason: MarketQuoteIssue) -> bool:
    """判断该失败是否应当冷却整个来源。

    **证券级失败返回 ``False``。** 一个坏符号（无数据、标的不支持、报价形状异常）
    只说明这个符号有问题，不代表来源不可用；拿去冷却来源会让同来源的其他符号一起失败。

    @param reason: 失败原因。
    @returns 是否冷却来源。
    """
    return reason not in PER_INSTRUMENT_ISSUES
