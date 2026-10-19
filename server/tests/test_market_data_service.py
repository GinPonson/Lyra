"""行情读取服务测试：缓存、换源与过期回退。

最要紧的一条：**主来源的过期缓存不终止换源**。

> A healthy explicitly configured fallback wins over a failed primary's stale cache.

一个能给出新鲜报价的备来源，比一个只能给出旧报价的主来源更有价值。
"""

from __future__ import annotations

from datetime import UTC

from lyra.application.market_budget import MarketProviderPolicy
from lyra.application.market_data_service import (
    MarketDataService,
    ProviderQuote,
)
from lyra.domain.market_data.history import MarketHistoryBar, MarketHistoryResult
from lyra.domain.market_data.stock_summary import (
    MarketPreviousClose,
    MarketStockSummary,
)
from lyra.domain.market_data.types import (
    MarketPricePoint,
    MarketQuote,
    MarketTiming,
    OptionInstrument,
    StockInstrument,
)
from lyra.infrastructure.market_data.alpaca_provider import AlpacaMarketDataProvider

NOW = 1_760_000_000_000
# 超过默认 7 天时效的报价时点（回归用；默认值见 market_data_service.DEFAULT_MAX_QUOTE_AGE_MS）。
_STALE_AGE_MS = 8 * 86_400_000
INSTRUMENT = StockInstrument(symbol="INTC")
POLICY = MarketProviderPolicy(
    cache_ttl_ms=30_000, max_requests_per_minute=10, max_requests_per_day=None
)


def _quote(*, bid: str = "12.3", ask: str = "12.4", as_of: str = "2025-10-09T08:53:20.000Z"):
    return MarketQuote(
        instrument_key="US:stock:INTC", provider_id="p", feed="f", currency="USD",
        bid=MarketPricePoint(bid, as_of), ask=MarketPricePoint(ask, as_of),
        last=None, close=None,
        timing=MarketTiming(kind="delayed", delay_minutes=15), fetched_at=as_of,
    )


def _iso(milliseconds: int) -> str:
    from datetime import datetime

    return (
        datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class Catalog:
    """按来源记录调用与预设结果。"""

    def __init__(self, results: dict) -> None:
        self.results = results
        self.calls: list[str] = []

    def provider(self, provider_id: str):
        def fetch(instrument, *, now_ms):
            self.calls.append(provider_id)
            result = self.results[provider_id]
            return result(now_ms) if callable(result) else result
        return fetch


def _service(catalog: Catalog, *, route=("primary", "secondary"), now=None, **kwargs):
    clock = now or (lambda: NOW)
    return MarketDataService(
        providers={name: catalog.provider(name) for name in catalog.results},
        routes={"stock": route}, policies={name: POLICY for name in catalog.results},
        now=clock, **kwargs,
    )


def _available(as_of: str = None):
    return ProviderQuote(status="available", quote=_quote(as_of=as_of or _iso(NOW)))


def _unavailable(reason: str):
    return ProviderQuote(status="unavailable", reason=reason)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 基本路径
# --------------------------------------------------------------------------


def test_returns_an_available_quote() -> None:
    """主来源给出新鲜报价时直接返回。"""
    catalog = Catalog({"primary": _available()})
    result = _service(catalog).get_quote(INSTRUMENT)

    assert result.status == "available"
    assert result.quote is not None
    assert result.reference_price is not None
    assert result.reference_price.basis == "mid"
    assert catalog.calls == ["primary"]


def test_uses_the_cache_on_the_second_read() -> None:
    """第二次读取命中缓存，不再打上游。"""
    catalog = Catalog({"primary": _available()})
    service = _service(catalog)

    service.get_quote(INSTRUMENT)
    second = service.get_quote(INSTRUMENT)

    assert catalog.calls == ["primary"]
    assert second.cached is True


def test_failover_to_the_secondary() -> None:
    """主来源不可用时用备来源。"""
    catalog = Catalog({"primary": _unavailable("provider_unavailable"), "secondary": _available()})
    result = _service(catalog).get_quote(INSTRUMENT)

    assert result.status == "available"
    assert catalog.calls == ["primary", "secondary"]


def test_no_route_configured_is_not_configured() -> None:
    """没有路由时返回 not_configured。"""
    catalog = Catalog({"primary": _available()})
    result = _service(catalog, route=()).get_quote(INSTRUMENT)

    assert result.status == "unavailable"
    assert result.issue == "not_configured"


def test_route_pointing_at_an_unassembled_provider_is_skipped() -> None:
    """路由指向未装配的来源时跳过，而不是崩溃。"""
    catalog = Catalog({"secondary": _available()})
    result = _service(catalog, route=("ghost", "secondary")).get_quote(INSTRUMENT)

    assert result.status == "available"
    assert catalog.calls == ["secondary"]


# --------------------------------------------------------------------------
# 过期回退（核心设计）
# --------------------------------------------------------------------------


def test_stale_primary_does_not_stop_the_fallback() -> None:
    """**主来源的过期缓存不终止换源。**

    一个能给出新鲜报价的备来源胜过只能给出旧报价的主来源。
    """
    old = _iso(NOW - _STALE_AGE_MS)  # 八天前：超过默认七天时效

    catalog = Catalog({
        "primary": ProviderQuote(status="available", quote=_quote(as_of=old)),
        "secondary": _available(),
    })
    result = _service(catalog).get_quote(INSTRUMENT)

    assert result.status == "available", "主来源的过期报价终止了换源"
    assert catalog.calls == ["primary", "secondary"]


def test_one_hour_old_quote_is_still_available() -> None:
    """**回归：默认时效是 7 天，不是 15 分钟。**

    15 分钟的默认值会把周内的延迟报价整片判成过期，让本可用的报价降级为 stale，
    并触发无谓的备来源请求与预算消耗。参考实现的默认值是 ``?? 7 * DAY_MS``。
    """
    one_hour_old = _iso(NOW - 60 * 60_000)
    catalog = Catalog({
        "primary": ProviderQuote(status="available", quote=_quote(as_of=one_hour_old)),
    })
    result = _service(catalog).get_quote(INSTRUMENT)

    assert result.status == "available"
    assert catalog.calls == ["primary"]


def test_returns_stale_when_no_provider_has_a_fresh_quote() -> None:
    """所有来源都只有过期报价时才返回过期结果。"""
    old = _iso(NOW - _STALE_AGE_MS)
    catalog = Catalog({
        "primary": ProviderQuote(status="available", quote=_quote(as_of=old)),
        "secondary": _unavailable("provider_unavailable"),
    })
    result = _service(catalog).get_quote(INSTRUMENT)

    assert result.status == "stale"
    assert result.quote is not None


def test_first_stale_result_is_kept() -> None:
    """多个过期结果时保留**第一个**（即最靠前的来源）。"""
    old = _iso(NOW - _STALE_AGE_MS)
    catalog = Catalog({
        "primary": ProviderQuote(status="available", quote=_quote(bid="1.0", ask="1.1", as_of=old)),
        "secondary": ProviderQuote(status="available", quote=_quote(bid="2.0", ask="2.1", as_of=old)),
    })
    result = _service(catalog).get_quote(INSTRUMENT)

    assert result.status == "stale"
    assert result.reference_price is not None
    assert result.reference_price.value == "1.05"  # (1.0 + 1.1) / 2


def test_quote_without_a_reference_price_is_skipped() -> None:
    """有报价但选不出参考价时继续尝试下一个来源。"""
    catalog = Catalog({
        "primary": ProviderQuote(status="available", quote=MarketQuote(
            instrument_key="k", provider_id="p", feed="f", currency="USD",
            bid=None, ask=None, last=None, close=None,
            timing=MarketTiming(kind="delayed", delay_minutes=15), fetched_at=_iso(NOW),
        )),
        "secondary": _available(),
    })
    result = _service(catalog).get_quote(INSTRUMENT)

    assert result.status == "available"
    assert catalog.calls == ["primary", "secondary"]


def test_crossed_market_is_rejected_as_invalid_response() -> None:
    """**交叉盘口（``ask < bid``）是数据形状错误，不是"没有数据"。**

    参考实现显式把它映射为 ``invalid_response``。若按可用报价放行，它会进入缓存并
    参与估值——而那个价格在市场上从未存在过。
    """
    crossed = ProviderQuote(status="available", quote=_quote(bid="12.4", ask="12.3"))
    catalog = Catalog({"primary": crossed})
    service = _service(catalog, route=("primary",))

    result = service.get_quote(INSTRUMENT)

    assert result.status == "unavailable"
    assert result.issue == "invalid_response"
    assert result.quote is None
    # 证券级失败不冷却来源：另一个符号仍然真的打上游。
    service.get_quote(StockInstrument(symbol="AMD"))
    assert catalog.calls == ["primary", "primary"]


# --------------------------------------------------------------------------
# 失败缓存与来源冷却
# --------------------------------------------------------------------------


def test_failure_is_cached_to_avoid_hammering_the_provider() -> None:
    """失败也会缓存——避免对同一个坏符号反复打上游。"""
    catalog = Catalog({"primary": _unavailable("no_data")})
    service = _service(catalog)

    service.get_quote(INSTRUMENT)
    service.get_quote(INSTRUMENT)

    assert catalog.calls == ["primary"]


def test_per_instrument_failure_does_not_block_the_provider() -> None:
    """**证券级失败不冷却来源**：另一个符号仍能正常取价。"""
    other = StockInstrument(symbol="AMD")
    catalog = Catalog({"primary": _unavailable("no_data")})
    service = _service(catalog, route=("primary",))

    service.get_quote(INSTRUMENT)          # 触发 no_data
    service.get_quote(other)               # 另一个符号

    # 第二次仍然真的打了上游，说明来源没有被冷却。
    assert catalog.calls == ["primary", "primary"]


def test_provider_level_failure_blocks_the_provider() -> None:
    """来源级失败冷却整个来源：冷却期内不再打上游。"""
    catalog = Catalog({"primary": _unavailable("credentials_rejected")})
    service = _service(catalog, route=("primary",))

    service.get_quote(INSTRUMENT)
    # 缓存过期后，冷却仍在生效期内。
    service._cache.clear()
    service.get_quote(INSTRUMENT)

    assert catalog.calls == ["primary"]


def test_network_failure_is_a_data_state_not_an_exception() -> None:
    """**网络失败是数据状态，不是异常**——行情缺失不能让账本读取失败。"""
    def exploding(instrument, *, now_ms):
        raise RuntimeError("connection reset")

    service = MarketDataService(
        providers={"primary": exploding}, routes={"stock": ("primary",)},
        policies={"primary": POLICY}, now=lambda: NOW,
    )
    result = service.get_quote(INSTRUMENT)

    assert result.status == "unavailable"
    assert result.issue == "provider_unavailable"


def test_cache_is_bounded() -> None:
    """缓存有界：超出上限时淘汰最旧条目。"""
    catalog = Catalog({"primary": _available()})
    service = _service(catalog, max_cache_entries=2)

    for symbol in ("A", "B", "C"):
        service.get_quote(StockInstrument(symbol=symbol))

    assert len(service._cache) <= 2


def test_per_call_route_does_not_clear_the_cache() -> None:
    """按调用显式换路由**不清空缓存**：缓存键带来源 id，旧条目不会被误用。

    多用户下路由不再是服务状态（``set_routes`` 已删除），而是 :meth:`read_quote` 的
    参数；每次调用各带自己的路由，缓存仍按公开报价身份（来源 + 合约）复用，因此换路由
    不会在切换瞬间打出一批上游请求。
    """
    catalog = Catalog({"primary": _available(), "secondary": _available()})
    service = _service(catalog)
    service.read_quote(INSTRUMENT, ("primary",))
    primary_keys = set(service._cache)
    # 主来源的报价确实进了缓存。
    assert len(primary_keys) == 1

    result = service.read_quote(INSTRUMENT, ("secondary", "primary"))

    assert result.status == "available"
    # 旧来源的条目仍在：换路由**没有清空缓存**。
    assert primary_keys <= set(service._cache)
    # 缓存键带来源 id：两个来源各留一条，不会互相误用。
    assert len(service._cache) == len(primary_keys) + 1
    # 第二次读命中的是新来源的刷新，而不是主来源的缓存。
    assert catalog.calls == ["primary", "secondary"]


def test_history_cache_is_public_bounded_and_preserves_fetch_cutoff() -> None:
    """历史缓存按公开身份/range 复用、有界，并保留原始抓取时点。"""
    calls: list[str] = []

    def fetch(instrument, *, history_range, now_ms, consume_page):
        calls.append(history_range)
        return MarketHistoryResult(
            status="available",
            bars=(MarketHistoryBar(
                timestamp="2025-10-09T08:00:00.000Z",
                open="12", high="13", low="11", close="12.5", volume=4,
            ),),
            feed="sip",
            fetched_at="2025-10-09T08:53:20.000Z",
            cutoff="2025-10-09T08:37:20.000Z",
        )

    service = MarketDataService(
        providers={}, history_providers={"primary": fetch},
        routes={}, policies={"primary": POLICY}, now=lambda: NOW,
        max_cache_entries=1,
    )

    first = service.read_history(INSTRUMENT, "1w", ("primary",))
    cached = service.read_history(INSTRUMENT, "1w", ("primary",))
    service.read_history(INSTRUMENT, "1m", ("primary",))

    assert first.cached is False
    assert cached.cached is True
    assert cached.cutoff == "2025-10-09T08:37:20.000Z"
    assert cached.fetched_at == "2025-10-09T08:53:20.000Z"
    assert calls == ["1w", "1m"]
    assert len(service._history_cache) == 1
    assert all("user" not in key and "account" not in key for key in service._history_cache)


def test_history_and_quote_share_the_provider_budget() -> None:
    """同一来源的历史请求与报价共用部署级请求预算。"""
    policy = MarketProviderPolicy(
        cache_ttl_ms=30_000, max_requests_per_minute=1, max_requests_per_day=None
    )
    history_calls: list[str] = []

    def history_fetch(instrument, *, history_range, now_ms, consume_page):
        history_calls.append(history_range)
        return MarketHistoryResult(status="unavailable", issue="no_data")

    service = MarketDataService(
        providers={"primary": lambda instrument, *, now_ms: _available()},
        history_providers={"primary": history_fetch},
        routes={"stock": ("primary",)}, policies={"primary": policy},
        now=lambda: NOW,
    )

    service.read_quote(INSTRUMENT, ("primary",))
    history = service.read_history(INSTRUMENT, "1w", ("primary",))

    assert history.status == "unavailable"
    assert history.issue == "rate_limited"
    assert history_calls == []
    assert len(service._budgets["primary"].minute_calls) == 1


def test_history_pagination_consumes_each_additional_shared_budget_unit() -> None:
    """额外历史页通过同一个 ProviderBudget 消费额度。"""
    policy = MarketProviderPolicy(
        cache_ttl_ms=30_000, max_requests_per_minute=1, max_requests_per_day=None
    )
    page_attempts: list[int] = []

    def history_fetch(instrument, *, history_range, now_ms, consume_page):
        denial = consume_page()
        page_attempts.append(now_ms)
        return MarketHistoryResult(
            status="unavailable", issue=denial or "invalid_response"
        )

    service = MarketDataService(
        providers={}, history_providers={"primary": history_fetch},
        routes={}, policies={"primary": policy}, now=lambda: NOW,
    )

    result = service.read_history(INSTRUMENT, "1w", ("primary",))

    assert result.status == "unavailable"
    assert result.issue == "rate_limited"
    assert len(page_attempts) == 1
    assert len(service._budgets["primary"].minute_calls) == 1


def test_expired_history_cache_keeps_expiry_cutoff_and_original_fetch_time() -> None:
    """缓存命中过期合约时保留到期锚点与原始 provider 抓取时间。"""
    clock = [NOW]
    calls: list[str] = []
    symbol = "INTC250321C00100000"

    def fetch(url, *, headers, timeout):
        calls.append(url)
        return type("Response", (), {
            "status": 200,
            "headers": {},
            "chunks": lambda self: iter([(
                '{"bars":{"' + symbol + '":[{"t":"2025-03-21T04:00:00Z",'
                '"o":1.4,"h":1.7,"l":1.3,"c":1.5,"v":5}]},'
                '"next_page_token":null}'
            ).encode()]),
        })()

    alpaca = AlpacaMarketDataProvider(
        api_key="key", secret_key="secret", fetch=fetch
    )
    service = MarketDataService(
        providers={}, history_providers={"alpaca": alpaca.fetch_history},
        routes={}, policies={
            "alpaca": MarketProviderPolicy(
                cache_ttl_ms=15 * 60_000, max_requests_per_minute=10,
                max_requests_per_day=None,
            )
        },
        now=lambda: clock[0],
    )
    instrument = OptionInstrument(
        underlying="INTC", expiry="2025-03-21", strike="100", option_type="C"
    )

    first = service.read_history(instrument, "1m", ("alpaca",))
    clock[0] += 30_000
    cached = service.read_history(instrument, "1m", ("alpaca",))

    assert first.status == "available"
    assert cached.cached is True
    assert first.cutoff == cached.cutoff == "2025-03-22T04:00:00.000Z"
    assert first.fetched_at == cached.fetched_at == "2025-10-09T08:53:20.000Z"
    assert calls and len(calls) == 1


def test_stock_summary_cache_uses_public_stock_identity_and_shared_two_request_budget() -> None:
    """同一正股摘要跨调用共享缓存；snapshot 与前收盘各消费一次预算。"""
    calls: list[str] = []

    def fetch(instrument, *, now_ms, consume_request):
        calls.append(instrument.symbol)
        assert consume_request() is None
        return MarketStockSummary(
            status="available",
            price=MarketPricePoint("12.5", "2025-10-09T08:30:00.000Z"),
            previous_close=MarketPreviousClose("12", "2025-10-08"),
            change_percent="4.166666666666666666666666666666666666666666666666666666666666667",
            feed="alpaca-test",
            fetched_at=_iso(now_ms),
        )

    service = MarketDataService(
        providers={}, stock_summary_providers={"alpaca": fetch}, routes={},
        policies={"alpaca": POLICY}, now=lambda: NOW,
    )

    first = service.read_stock_summary(INSTRUMENT, ("alpaca",))
    second = service.read_stock_summary(INSTRUMENT, ("alpaca",))

    assert first.status == second.status == "available"
    assert second.cached is True
    assert first.fetched_at == second.fetched_at
    assert first.price == second.price
    assert calls == ["INTC"]
    assert len(service._budgets["alpaca"].minute_calls) == 2
    assert len(service._stock_summary_cache) == 1


def test_partial_stock_summary_uses_short_cooldown_and_keeps_original_times() -> None:
    """缺少前收盘的部分成功只短暂缓存，缓存结果保留原始成交与抓取时间。"""
    clock = [NOW]
    calls: list[int] = []

    def fetch(instrument, *, now_ms, consume_request):
        calls.append(now_ms)
        assert consume_request() is None
        return MarketStockSummary(
            status="available",
            price=MarketPricePoint("12.5", _iso(now_ms - 60_000)),
            change_issue="no_previous_close",
            feed="alpaca-test",
            fetched_at=_iso(now_ms),
        )

    service = MarketDataService(
        providers={}, stock_summary_providers={"alpaca": fetch}, routes={},
        policies={"alpaca": POLICY}, now=lambda: clock[0],
    )

    first = service.read_stock_summary(INSTRUMENT, ("alpaca",))
    clock[0] += 30_000
    cached = service.read_stock_summary(INSTRUMENT, ("alpaca",))
    clock[0] += 31_000
    refreshed = service.read_stock_summary(INSTRUMENT, ("alpaca",))

    assert first.change_issue == cached.change_issue == "no_previous_close"
    assert cached.cached is True
    assert cached.price == first.price
    assert cached.fetched_at == first.fetched_at
    assert refreshed.cached is False
    assert len(calls) == 2


def test_stock_summary_respects_explicit_fallbacks_and_reports_unsupported_sources() -> None:
    """只遍历显式路由：quote-only 来源不伪装成摘要能力。"""

    def fetch(instrument, *, now_ms, consume_request):
        return MarketStockSummary(
            status="available",
            price=MarketPricePoint("12.5", _iso(now_ms)),
            previous_close=MarketPreviousClose("12", "2025-10-08"),
            change_percent="4.166666666666666666666666666666666666666666666666666666666666667",
            fetched_at=_iso(now_ms),
        )

    service = MarketDataService(
        providers={}, stock_summary_providers={"alpaca": fetch}, routes={},
        policies={"alpaca": POLICY}, now=lambda: NOW,
    )

    fallback = service.read_stock_summary(INSTRUMENT, ("marketdata", "alpaca"))
    unsupported = service.read_stock_summary(INSTRUMENT, ("marketdata",))

    assert fallback.status == "available"
    assert fallback.provider_id == "alpaca"
    assert unsupported.status == "unavailable"
    assert unsupported.issue == "summary_not_supported"
    assert unsupported.provider_id is None
