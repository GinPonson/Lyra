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
from ..domain.market_data.history import (
    MarketHistoryBar,
    MarketHistoryRange,
    MarketHistoryResult as ProviderHistory,
)
from ..domain.market_data.stock_summary import (
    MarketStockSummary,
    MarketPreviousClose,
)
from ..domain.market_data.types import (
    MarketInstrument,
    MarketPricePoint,
    MarketQuote,
    MarketQuoteIssue,
    StockInstrument,
    market_instrument_key,
)
from .market_budget import (
    DEFAULT_COOLDOWN_MS,
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


@dataclass(frozen=True)
class MarketHistoryLookup:
    """历史行情路由与缓存读取结果。"""

    status: Literal["available", "unavailable"]
    bars: tuple[MarketHistoryBar, ...]
    issue: str | None
    cached: bool
    provider_id: str | None
    feed: str | None
    fetched_at: str
    cutoff: str


@dataclass(frozen=True)
class MarketStockSummaryLookup:
    """共享缓存与用户显式来源路由返回的正股成交摘要。"""

    status: Literal["available", "unavailable"]
    price: MarketPricePoint | None
    previous_close: MarketPreviousClose | None
    change_percent: str | None
    change_issue: str | None
    issue: str | None
    cached: bool
    provider_id: str | None
    feed: str | None
    fetched_at: str


@dataclass
class _CacheEntry:
    """一条缓存。失败也会缓存——避免对同一个坏符号反复打上游。"""

    quote: MarketQuote | None
    issue: MarketQuoteIssue | None
    expires_at: int


@dataclass
class _HistoryCacheEntry:
    """公开身份对应的历史结果；不保存任何用户或账户信息。"""

    history: ProviderHistory
    expires_at: int


@dataclass
class _StockSummaryCacheEntry:
    """公开股票身份对应的最新成交摘要缓存。"""

    summary: MarketStockSummary
    expires_at: int


# **7 天，不是 15 分钟。** 参考实现的默认值是 ``?? 7 * DAY_MS``，两侧装配点都没有
# 覆盖它；15 分钟会把"周内的延迟报价"整片判成过期，进而让本可用的报价降级为 stale、
# 触发无谓的备来源请求与预算消耗。
DEFAULT_MAX_QUOTE_AGE_MS = 7 * 86_400_000
# 参考实现的默认值是 ``?? 1_000``；512 会在长会话里更早淘汰仍然新鲜的报价。
DEFAULT_MAX_CACHE_ENTRIES = 1_000


class MarketDataService:
    """按路由读取行情。

    **本服务是部署级共享的单例**：公开报价缓存与各来源的请求预算都挂在它上面。
    ``routes`` 只是装配默认值，供 :meth:`get_quote` 在没有显式路由时使用；**多用户下
    按用户设置读行情必须把路由显式传给 :meth:`read_quote`**，因为缓存键带来源 id 与
    合约标识（公开报价身份），与"谁在问"无关，所以不同用户可以复用同一条缓存。

    @param providers: 来源 id → 取报价的函数。
    @param stock_summary_providers: 来源 id → 读取正股最新成交与前收盘摘要的函数。
    @param routes: 资产类型 → 来源 id 顺序（**顺序即主备**）；仅作装配默认值。
    @param policies: 来源 id → 策略。
    @param now: 当前毫秒时间戳提供者。
    @param max_quote_age_ms: 参考价超过该时长即视为过期。
    @param max_cache_entries: 缓存条目上限。
    """

    def __init__(
        self,
        *,
        providers: Mapping[str, Callable[..., ProviderQuote]],
        history_providers: Mapping[str, Callable[..., ProviderHistory]] | None = None,
        stock_summary_providers: Mapping[str, Callable[..., MarketStockSummary]] | None = None,
        routes: Mapping[str, tuple[str, ...]],
        policies: Mapping[str, MarketProviderPolicy],
        now: Callable[[], int],
        max_quote_age_ms: int = DEFAULT_MAX_QUOTE_AGE_MS,
        max_cache_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
    ) -> None:
        self._providers = dict(providers)
        self._history_providers = dict(history_providers or {})
        self._stock_summary_providers = dict(stock_summary_providers or {})
        self._routes = dict(routes)
        self._policies = dict(policies)
        self._now = now
        self._max_quote_age_ms = max_quote_age_ms
        self._max_cache_entries = max_cache_entries
        # **缓存是公开报价身份的函数，不是用户的函数**：键由来源 id 与合约标识构成，
        # 同一合约在两个用户之间可以复用同一条报价。按用户分片缓存等于把部署级的上游
        # 额度乘以用户数，反而击穿缓存的意义（计划 §4.4）。
        self._cache: dict[str, _CacheEntry] = {}
        # 历史缓存也只由来源、完整证券身份、range 与时间桶构成，跨用户复用公开 bars。
        self._history_cache: dict[str, _HistoryCacheEntry] = {}
        # 最新成交摘要独立缓存，避免把涨跌基准混入 quote/估值缓存。
        self._stock_summary_cache: dict[str, _StockSummaryCacheEntry] = {}
        # 网络调用可并行；共享缓存、路由与预算则通过状态锁读写。
        self._state_lock = RLock()
        # 同一来源与合约只允许一个刷新请求；固定条带数避免锁表随符号数增长。
        self._instrument_locks = tuple(Lock() for _ in range(64))
        # **预算是部署级共享的**（每来源一份，不是每用户一份）：若按用户复制服务，
        # 每个用户都会各自拿到全额额度，实际上游请求量被放大到用户数倍，限流形同虚设。
        self._budgets: dict[str, ProviderBudget] = {
            provider_id: ProviderBudget()
            for provider_id in (self._providers.keys() | self._history_providers.keys())
            | self._stock_summary_providers.keys()
        }

    def get_quote(self, instrument: MarketInstrument) -> MarketQuoteLookup:
        """按**装配默认**路由读取报价。

        仅用于装配默认值场景（例如来源目录自检）。多用户下按用户设置读取必须用
        :meth:`read_quote` 显式传入该用户的路由——本方法不读任何用户设置。

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
        """按**调用方显式给出的**路由读取报价。

        路由是参数而不是服务状态：调用方（HTTP 路由或 Agent 工具）先读取当前用户的
        设置快照，再把该用户的路由传进来，因此并发的不同用户不会互相改写来源。

        @param instrument: 内部合约。
        @param route: 来源顺序（主备顺序）。
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

    def read_history(
        self,
        instrument: MarketInstrument,
        history_range: MarketHistoryRange,
        route: tuple[str, ...],
    ) -> MarketHistoryLookup:
        """按用户本次显式来源路由读取公开历史 bars。

        未支持历史的来源只会产生 ``history_not_supported``；服务仅尝试用户明确选中的
        后续来源，不会暗中转向未选来源。历史请求消费与报价共用的供应商预算和锁条带。

        @param instrument: 从当前授权持仓推导出的完整合约。
        @param history_range: 客户端允许的有限历史窗口。
        @param route: 同一数据库快照中的用户来源顺序。
        @returns 历史结果；来源问题作为数据状态返回。
        """
        now = self._now()
        requested_cutoff = _iso(now - 16 * 60_000)
        issue: str | None = "not_configured" if not route else None
        identifier = market_instrument_key(instrument)
        tried = False
        last_history: ProviderHistory | None = None
        last_provider_id: str | None = None
        last_cached = False

        for provider_id in route:
            provider = self._history_providers.get(provider_id)
            if provider is None:
                # 没有历史能力的已配置来源不得被 quote 接口伪装成历史数据来源。
                issue = "history_not_supported"
                continue
            tried = True
            key = _history_cache_key(provider_id, identifier, history_range, now)
            history, cached = self._history_entry_for(
                provider_id, provider, instrument, history_range, key
            )
            last_history = history
            last_provider_id = provider_id
            last_cached = cached
            if history.status == "available":
                return MarketHistoryLookup(
                    status="available", bars=history.bars, issue=None,
                    cached=cached, provider_id=provider_id, feed=history.feed,
                    fetched_at=history.fetched_at or _iso(now),
                    cutoff=history.cutoff or requested_cutoff,
                )
            issue = history.issue or "provider_unavailable"

        return MarketHistoryLookup(
            status="unavailable", bars=(), issue=(
                issue or ("provider_unavailable" if tried else "history_not_supported")
            ), cached=last_cached,
            provider_id=last_provider_id,
            feed=None if last_history is None else last_history.feed,
            fetched_at=(
                _iso(now) if last_history is None or last_history.fetched_at is None
                else last_history.fetched_at
            ),
            cutoff=(
                requested_cutoff if last_history is None or last_history.cutoff is None
                else last_history.cutoff
            ),
        )

    def read_stock_summary(
        self, instrument: StockInstrument, route: tuple[str, ...]
    ) -> MarketStockSummaryLookup:
        """按用户显式来源路由读取正股成交与前收盘摘要。

        摘要缓存只由来源和公开股票身份组成；多账户或多个期权持仓引用同一标的时，
        会共享缓存与同一来源预算。来源若不具备该能力，只报告 ``summary_not_supported``
        并继续检查用户显式配置的后续来源。

        @param instrument: 从当前授权持仓推导出的已校验正股身份。
        @param route: 同一数据库快照中的当前用户正股来源顺序。
        @returns 成交摘要或稳定不可用状态；网络错误不向调用方抛出。
        """
        now = self._now()
        identifier = market_instrument_key(instrument)
        issue: str | None = "not_configured" if not route else None
        partial: MarketStockSummaryLookup | None = None
        last_provider_id: str | None = None
        last_feed: str | None = None
        last_fetched_at = _iso(now)
        last_cached = False
        attempted_provider = False

        for provider_id in route:
            provider = self._stock_summary_providers.get(provider_id)
            if provider is None:
                # Quote-only 来源不能被伪装成最新成交/前收盘来源。
                if not attempted_provider:
                    issue = "summary_not_supported"
                continue
            attempted_provider = True
            key = f"{provider_id}\u0000stock-summary\u0000{identifier}"
            summary, cached = self._stock_summary_entry_for(
                provider_id, provider, instrument, key
            )
            last_provider_id = provider_id
            last_feed = summary.feed
            last_cached = cached
            last_fetched_at = summary.fetched_at or last_fetched_at
            if summary.status == "available":
                lookup = MarketStockSummaryLookup(
                    status="available",
                    price=summary.price,
                    previous_close=summary.previous_close,
                    change_percent=summary.change_percent,
                    change_issue=summary.change_issue,
                    issue=None,
                    cached=cached,
                    provider_id=provider_id,
                    feed=summary.feed,
                    fetched_at=summary.fetched_at or _iso(now),
                )
                # 不混用两个供应商的成交和前收盘；若主来源仅缺前收盘，可以整体尝试
                # 显式备来源，最终仍完整返回同一供应商的一组价格。
                if summary.change_issue is None and summary.previous_close is not None:
                    return lookup
                if partial is None:
                    partial = lookup
                continue
            issue = summary.issue or "provider_unavailable"

        if partial is not None:
            return partial
        return MarketStockSummaryLookup(
            status="unavailable",
            price=None,
            previous_close=None,
            change_percent=None,
            change_issue=None,
            issue=issue or "provider_unavailable",
            cached=last_cached,
            provider_id=last_provider_id,
            feed=last_feed,
            fetched_at=last_fetched_at,
        )

    def _history_entry_for(
        self,
        provider_id: str,
        provider: Callable[..., ProviderHistory],
        instrument: MarketInstrument,
        history_range: MarketHistoryRange,
        key: str,
    ) -> tuple[ProviderHistory, bool]:
        """读取历史缓存，或在来源级预算与合约条带锁下刷新并缓存。

        @param provider_id: 来源标识，用于找到共享预算与缓存策略。
        @param provider: 该来源的历史读取函数。
        @param instrument: 完整股票或期权身份。
        @param history_range: 限定的历史窗口。
        @param key: 不含用户信息的公开历史缓存键。
        @returns 历史结果及其是否来自缓存。
        """
        now = self._now()
        with self._state_lock:
            existing = self._history_cache.get(key)
            if existing is not None and existing.expires_at > now:
                self._history_cache.pop(key)
                self._history_cache[key] = existing
                return existing.history, True

        stripe = self._instrument_locks[hash(key) % len(self._instrument_locks)]
        with stripe:
            now = self._now()
            with self._state_lock:
                existing = self._history_cache.get(key)
                if existing is not None and existing.expires_at > now:
                    self._history_cache.pop(key)
                    self._history_cache[key] = existing
                    return existing.history, True
                policy = self._policies[provider_id]
                budget = self._budgets[provider_id]
                denied = budget.consume(policy, now)

            if denied is not None:
                history = ProviderHistory(status="unavailable", issue=denied)
            else:
                try:
                    history = provider(
                        instrument,
                        history_range=history_range,
                        now_ms=now,
                        consume_page=lambda: self._consume_history_page(provider_id),
                    )
                    if (
                        history.status == "available"
                        and not history.bars
                    ):
                        history = ProviderHistory(
                            status="unavailable", issue="invalid_response"
                        )
                    if history.status == "available" and any(
                        left.timestamp >= right.timestamp
                        for left, right in zip(history.bars, history.bars[1:])
                    ):
                        history = ProviderHistory(
                            status="unavailable", issue="invalid_response"
                        )
                except Exception:  # noqa: BLE001 - 来源协议/实现异常作为稳定数据状态返回
                    history = ProviderHistory(
                        status="unavailable", issue="provider_unavailable"
                    )

            completed = self._now()
            if history.status == "available":
                expires_at = completed + policy.cache_ttl_ms
            else:
                reason = history.issue or "provider_unavailable"
                cooldown_reason = reason if reason in {
                    "rate_limited", "credentials_rejected", "no_data",
                    "unsupported_instrument", "currency_unsupported", "invalid_response",
                    "timeout",
                } else "provider_unavailable"
                cooldown = cooldown_ms(cooldown_reason)  # type: ignore[arg-type]
                if denied is None and should_block_provider(cooldown_reason):  # type: ignore[arg-type]
                    with self._state_lock:
                        budget.block(completed + cooldown, cooldown_reason)  # type: ignore[arg-type]
                expires_at = completed + cooldown

            with self._state_lock:
                self._history_cache.pop(key, None)
                self._history_cache[key] = _HistoryCacheEntry(
                    history=history, expires_at=expires_at
                )
                while len(self._history_cache) > self._max_cache_entries:
                    oldest = next(iter(self._history_cache), None)
                    if oldest is None:
                        break
                    del self._history_cache[oldest]
            return history, False

    def _consume_history_page(self, provider_id: str) -> str | None:
        """为附加历史页消费报价共用的部署级来源预算。

        @param provider_id: 需要扣减额度的来源标识。
        @returns 拒绝时的稳定 issue；获准时为 ``None``。
        """
        now = self._now()
        with self._state_lock:
            policy = self._policies[provider_id]
            return self._budgets[provider_id].consume(policy, now)

    def _stock_summary_entry_for(
        self,
        provider_id: str,
        provider: Callable[..., MarketStockSummary],
        instrument: StockInstrument,
        key: str,
    ) -> tuple[MarketStockSummary, bool]:
        """在共享预算与股票条带锁下读取或刷新公开摘要缓存。

        @param provider_id: 来源标识，用于读取共享预算和 TTL。
        @param provider: 该来源的摘要读取函数。
        @param instrument: 已验证股票身份。
        @param key: 不包含用户、账户或持仓的公开缓存键。
        @returns 来源摘要及缓存命中标记。
        """
        now = self._now()
        with self._state_lock:
            existing = self._stock_summary_cache.get(key)
            if existing is not None and existing.expires_at > now:
                self._stock_summary_cache.pop(key)
                self._stock_summary_cache[key] = existing
                return existing.summary, True

        stripe = self._instrument_locks[hash(key) % len(self._instrument_locks)]
        with stripe:
            now = self._now()
            with self._state_lock:
                existing = self._stock_summary_cache.get(key)
                if existing is not None and existing.expires_at > now:
                    self._stock_summary_cache.pop(key)
                    self._stock_summary_cache[key] = existing
                    return existing.summary, True
                policy = self._policies[provider_id]
                budget = self._budgets[provider_id]
                denied = budget.consume(policy, now)

            if denied is not None:
                summary = MarketStockSummary(status="unavailable", issue=denied)
            else:
                try:
                    summary = provider(
                        instrument,
                        now_ms=now,
                        consume_request=lambda: self._consume_history_page(provider_id),
                    )
                    if (
                        summary.status == "available"
                        and (
                            summary.price is None
                            or (summary.previous_close is None
                                and summary.change_percent is not None)
                            or (summary.previous_close is not None
                                and summary.change_percent is None)
                            or (summary.previous_close is None
                                and summary.change_issue is None)
                        )
                    ):
                        summary = MarketStockSummary(
                            status="unavailable", issue="invalid_response"
                        )
                except Exception:  # noqa: BLE001 - 来源异常作为稳定状态返回
                    summary = MarketStockSummary(
                        status="unavailable", issue="provider_unavailable"
                    )

            completed = self._now()
            if summary.status == "available":
                if summary.change_issue is None:
                    expires_at = completed + policy.cache_ttl_ms
                else:
                    # 部分摘要保留成交价，但缺少前收盘时很快重试，避免 15 分钟内
                    # 一直卡住无涨跌幅状态；原始 asOf/fetchedAt 会随缓存结果保留。
                    reason = summary.change_issue
                    cooldown_reason = reason if reason in {
                        "rate_limited", "credentials_rejected", "no_data",
                        "unsupported_instrument", "currency_unsupported", "invalid_response",
                        "timeout",
                    } else None
                    cooldown = (
                        cooldown_ms(cooldown_reason)  # type: ignore[arg-type]
                        if cooldown_reason is not None
                        else DEFAULT_COOLDOWN_MS
                    )
                    if (
                        denied is None
                        and cooldown_reason is not None
                        and should_block_provider(cooldown_reason)  # type: ignore[arg-type]
                    ):
                        with self._state_lock:
                            budget.block(
                                completed + cooldown, cooldown_reason  # type: ignore[arg-type]
                            )
                    expires_at = completed + cooldown
            else:
                reason = summary.issue or "provider_unavailable"
                cooldown_reason = (
                    reason if reason in {
                        "rate_limited", "credentials_rejected", "no_data",
                        "unsupported_instrument", "currency_unsupported", "invalid_response",
                        "timeout",
                    }
                    else "provider_unavailable"
                )
                cooldown = cooldown_ms(cooldown_reason)  # type: ignore[arg-type]
                if denied is None and should_block_provider(cooldown_reason):  # type: ignore[arg-type]
                    with self._state_lock:
                        budget.block(completed + cooldown, cooldown_reason)  # type: ignore[arg-type]
                expires_at = completed + cooldown

            with self._state_lock:
                self._stock_summary_cache.pop(key, None)
                self._stock_summary_cache[key] = _StockSummaryCacheEntry(
                    summary=summary, expires_at=expires_at
                )
                while len(self._stock_summary_cache) > self._max_cache_entries:
                    oldest = next(iter(self._stock_summary_cache), None)
                    if oldest is None:
                        break
                    del self._stock_summary_cache[oldest]
            return summary, False

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


def _history_cache_key(
    provider_id: str, instrument_key: str, history_range: MarketHistoryRange, now_ms: int
) -> str:
    """按公开合约、时间范围及 15 分钟窗口缓存，不携带用户或持仓信息。"""
    bucket = now_ms // (15 * 60_000)
    return f"{provider_id}\u0000history\u0000{instrument_key}\u0000{history_range}\u0000{bucket}"


def _iso(milliseconds: int) -> str:
    """把毫秒时间戳渲染为 UTC ISO 文本。"""
    from datetime import UTC, datetime

    return (
        datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
