"""行情读取服务：缓存、主备换源与过期回退。

抽自 TypeScript 版 ``application/market-data-service.ts`` 的读取循环。

**一条容易做错的设计**：主来源的过期缓存**不会**终止换源。

```
"A healthy explicitly configured fallback wins over a failed primary's stale cache."
```

若主来源只能给出过期报价，循环必须继续尝试备来源——一个能给出新鲜报价的备来源，
比一个只能给出旧报价的主来源更有价值。因此过期结果被暂存而不是立即返回，
只有在所有来源都给不出新鲜报价时才使用。

**另一条**：网络失败一律是"数据状态"，不是异常。行情缺失不能让账本读取失败——
账本本身是权威的，行情只是附加信息。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock, RLock
from typing import Literal

from ..domain.market_data.reference_price import (
    MarketReferencePrice,
    is_crossed_market,
    select_market_reference_price,
)
from ..domain.market_data.types import (
    MarketInstrument,
    MarketQuote,
    MarketQuoteIssue,
    StockInstrument,
    market_instrument_key,
)
from .market_budget import (
    MarketProviderPolicy,
    ProviderBudget,
    cooldown_ms,
    should_block_provider,
)

LookupStatus = Literal["available", "stale", "unavailable"]


@dataclass(frozen=True)
class ProviderQuote:
    """供应商返回的报价或原因。"""

    status: Literal["available", "unavailable"]
    quote: MarketQuote | None = None
    reason: MarketQuoteIssue | None = None


@dataclass(frozen=True)
class MarketQuoteLookup:
    """一次读取的结果。"""

    status: LookupStatus
    quote: MarketQuote | None
    reference_price: MarketReferencePrice | None
    issue: MarketQuoteIssue | None
    cached: bool


@dataclass
class _CacheEntry:
    """一条缓存。失败也会缓存——避免对同一个坏符号反复打上游。"""

    quote: MarketQuote | None
    issue: MarketQuoteIssue | None
    expires_at: int


# **7 天，不是 15 分钟。** 参考实现的默认值是 ``?? 7 * DAY_MS``，两侧装配点都没有
# 覆盖它；15 分钟会把"周内的延迟报价"整片判成过期，进而让本可用的报价降级为 stale、
# 触发无谓的备来源请求与预算消耗。
DEFAULT_MAX_QUOTE_AGE_MS = 7 * 86_400_000
# 参考实现的默认值是 ``?? 1_000``；512 会在长会话里更早淘汰仍然新鲜的报价。
DEFAULT_MAX_CACHE_ENTRIES = 1_000


class MarketDataService:
    """按路由读取行情。

    @param providers: 来源 id → 取报价的函数。
    @param routes: 资产类型 → 来源 id 顺序（**顺序即主备**）。
    @param policies: 来源 id → 策略。
    @param now: 当前毫秒时间戳提供者。
    @param max_quote_age_ms: 参考价超过该时长即视为过期。
    @param max_cache_entries: 缓存条目上限。
    """

    def __init__(
        self,
        *,
        providers: Mapping[str, Callable[..., ProviderQuote]],
        routes: Mapping[str, tuple[str, ...]],
        policies: Mapping[str, MarketProviderPolicy],
        now: Callable[[], int],
        max_quote_age_ms: int = DEFAULT_MAX_QUOTE_AGE_MS,
        max_cache_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
    ) -> None:
        self._providers = dict(providers)
        self._routes = dict(routes)
        self._policies = dict(policies)
        self._now = now
        self._max_quote_age_ms = max_quote_age_ms
        self._max_cache_entries = max_cache_entries
        self._cache: dict[str, _CacheEntry] = {}
        # 网络调用可并行；共享缓存、路由与预算则通过状态锁读写。
        self._state_lock = RLock()
        # 同一来源与合约只允许一个刷新请求；固定条带数避免锁表随符号数增长。
        self._instrument_locks = tuple(Lock() for _ in range(64))
        self._budgets: dict[str, ProviderBudget] = {
            provider_id: ProviderBudget() for provider_id in providers
        }

    def set_routes(self, routes: Mapping[str, tuple[str, ...]]) -> None:
        """热切换路由。

        **不清空缓存**：缓存键里带来源 id，因此切换后旧来源的条目不会被新来源误用；
        清空反而会让切换立刻打出一批上游请求。
        """
        with self._state_lock:
            self._routes = {
                asset_type: tuple(routes.get(asset_type, ()))
                for asset_type in ("stock", "option")
            }

    def get_quote(self, instrument: MarketInstrument) -> MarketQuoteLookup:
        """按资产类型的路由读取报价。

        @param instrument: 内部合约。
        @returns 读取结果；**永不抛异常**（网络失败是数据状态）。
        """
        asset_type = "stock" if isinstance(instrument, StockInstrument) else "option"
        with self._state_lock:
            route = self._routes.get(asset_type, ())
        return self.read_quote(instrument, route)

    def read_quote(
        self, instrument: MarketInstrument, route: tuple[str, ...]
    ) -> MarketQuoteLookup:
        """按给定路由读取报价。

        @param instrument: 内部合约。
        @param route: 来源顺序。
        @returns 读取结果。
        """
        issue: MarketQuoteIssue = "not_configured"
        stale: MarketQuoteLookup | None = None
        identifier = market_instrument_key(instrument)

        for provider_id in route:
            if provider_id not in self._providers:
                # 路由指向未装配的来源：跳过而不是崩溃。
                continue
            key = f"{provider_id}\u0000{identifier}"
            entry, cached = self._entry_for(provider_id, instrument, key)

            issue = entry.issue or "no_data"
            if entry.quote is None:
                continue
            reference = select_market_reference_price(entry.quote)
            if reference is None:
                continue
            now = self._now()
            too_old = now - _parse_millis(reference.as_of) > self._max_quote_age_ms
            result = MarketQuoteLookup(
                # 有 issue 说明这次刷新失败，只是用了缓存里的旧报价 → stale。
                status="available" if entry.issue is None and not too_old else "stale",
                quote=entry.quote,
                reference_price=reference,
                issue=entry.issue,
                cached=cached,
            )
            if result.status == "available":
                return result
            # 记下第一个过期结果，但**继续尝试其它来源**：
            # 一个能给出新鲜报价的备来源胜过只能给出旧报价的主来源。
            if stale is None:
                stale = result

        if stale is not None:
            return stale
        return MarketQuoteLookup(
            status="unavailable", quote=None, reference_price=None,
            issue=issue, cached=False,
        )

    def _refresh(
        self,
        provider_id: str,
        instrument: MarketInstrument,
        key: str,
    ) -> tuple[_CacheEntry, bool]:
        """刷新单一缓存键，并返回报价是否来自已有缓存。"""
        stripe = self._instrument_locks[hash(key) % len(self._instrument_locks)]
        with stripe:
            now = self._now()
            with self._state_lock:
                existing = self._cache.get(key)
                if existing is not None and existing.expires_at > now:
                    self._cache.pop(key)
                    self._cache[key] = existing
                    return existing, True
                previous = existing
                policy = self._policies[provider_id]
                budget = self._budgets[provider_id]
                denied = budget.consume(policy, now)

            if denied is not None:
                result = ProviderQuote(status="unavailable", reason=denied)
            else:
                try:
                    result = self._providers[provider_id](instrument, now_ms=now)
                except Exception:  # noqa: BLE001 - 供应商实现异常折叠为来源不可用
                    result = ProviderQuote(
                        status="unavailable", reason="provider_unavailable"
                    )

            completed = self._now()
            # 交叉盘口是供应商数据形状错误，不能让它进入估值缓存。
            if (
                result.status == "available"
                and result.quote is not None
                and is_crossed_market(result.quote)
            ):
                result = ProviderQuote(status="unavailable", reason="invalid_response")
            if result.status == "available" and result.quote is not None:
                entry = _CacheEntry(
                    quote=result.quote, issue=None,
                    expires_at=completed + policy.cache_ttl_ms,
                )
                from_cache = False
            else:
                reason: MarketQuoteIssue = result.reason or "provider_unavailable"
                cooldown = cooldown_ms(reason)
                # 预算状态也会被不同合约并发访问，冷却写入与额度消费共用状态锁。
                with self._state_lock:
                    if denied is None and should_block_provider(reason):
                        budget.block(completed + cooldown, reason)
                entry = _CacheEntry(
                    # 保留上一次报价作为过期回退，并明确标记它来自缓存。
                    quote=None if previous is None else previous.quote,
                    issue=reason,
                    expires_at=completed + cooldown,
                )
                from_cache = previous is not None and previous.quote is not None

            with self._state_lock:
                self._cache.pop(key, None)
                self._cache[key] = entry
                # 有界缓存按最近使用顺序淘汰。
                while len(self._cache) > self._max_cache_entries:
                    oldest = next(iter(self._cache), None)
                    if oldest is None:
                        break
                    del self._cache[oldest]
            return entry, from_cache

    def _entry_for(
        self, provider_id: str, instrument: MarketInstrument, key: str
    ) -> tuple[_CacheEntry, bool]:
        """读取未过期缓存，或按合约互斥刷新后返回结果。"""
        now = self._now()
        with self._state_lock:
            existing = self._cache.get(key)
            if existing is not None and existing.expires_at > now:
                self._cache.pop(key)
                self._cache[key] = existing
                return existing, True
        return self._refresh(provider_id, instrument, key)


def _parse_millis(value: str) -> int:
    """解析 ISO 时间戳为毫秒。"""
    from datetime import datetime

    return int(
        datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    )
