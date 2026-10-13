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
from lyra.domain.market_data.types import (
    MarketPricePoint,
    MarketQuote,
    MarketTiming,
    StockInstrument,
)

NOW = 1_760_000_000_000
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
    old = _iso(NOW - 60 * 60_000)  # 一小时前的报价，超过默认 15 分钟时效

    catalog = Catalog({
        "primary": ProviderQuote(status="available", quote=_quote(as_of=old)),
        "secondary": _available(),
    })
    result = _service(catalog).get_quote(INSTRUMENT)

    assert result.status == "available", "主来源的过期报价终止了换源"
    assert catalog.calls == ["primary", "secondary"]


def test_returns_stale_when_no_provider_has_a_fresh_quote() -> None:
    """所有来源都只有过期报价时才返回过期结果。"""
    old = _iso(NOW - 60 * 60_000)
    catalog = Catalog({
        "primary": ProviderQuote(status="available", quote=_quote(as_of=old)),
        "secondary": _unavailable("provider_unavailable"),
    })
    result = _service(catalog).get_quote(INSTRUMENT)

    assert result.status == "stale"
    assert result.quote is not None


def test_first_stale_result_is_kept() -> None:
    """多个过期结果时保留**第一个**（即最靠前的来源）。"""
    old = _iso(NOW - 60 * 60_000)
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


def test_set_routes_switches_hot_without_clearing_the_cache() -> None:
    """热切换路由**不清空缓存**：清空会让切换立刻打出一批上游请求。"""
    catalog = Catalog({"primary": _available(), "secondary": _available()})
    service = _service(catalog)
    service.get_quote(INSTRUMENT)
    before = len(service._cache)

    service.set_routes({"stock": ("secondary", "primary")})

    assert len(service._cache) == before
    # 切换后仍然可用（缓存键带来源 id，旧条目不会被误用）。
    assert service.get_quote(INSTRUMENT).status == "available"
