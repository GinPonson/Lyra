"""行情运行时装配测试。

两条要点：

1. **供应商按凭据注册，与路由无关**——一对齐全的凭据说明操作者配置了这个来源，
   界面需要展示"已配置但未启用"；
2. **historical 模式的缓存 TTL 是 24 小时**——收盘价当天不会变，15 分钟一次纯属浪费额度。
"""

from __future__ import annotations

from lyra.infrastructure.market_data.config import load_market_data_config
from lyra.infrastructure.market_data.runtime import (
    ALPACA_CACHE_TTL_MS,
    MARKETDATA_DELAYED_CACHE_TTL_MS,
    MARKETDATA_HISTORICAL_CACHE_TTL_MS,
    create_market_data_runtime,
)

ALPACA_CREDS = {"LYRA_ALPACA_API_KEY": "k", "LYRA_ALPACA_SECRET_KEY": "s"}
MARKETDATA_CREDS = {"LYRA_MARKETDATA_API_TOKEN": "t"}


def _runtime(**environment):
    config = load_market_data_config(environment)
    return create_market_data_runtime(config, fetch=lambda *a, **k: None, now=lambda: 0)


def test_no_credentials_means_no_providers() -> None:
    """没有凭据时不注册任何来源。"""
    runtime = _runtime()

    assert runtime.provider_ids == ()
    assert runtime.default_routes == {"stock": (), "option": ()}
    assert [source.id for source in runtime.provider_options] == ["alpaca", "marketdata"]
    assert all(source.configured is False for source in runtime.provider_options)


def test_provider_catalog_reports_real_timing_and_cache_settings() -> None:
    """设置目录完整列出来源，并反映实际凭据、时效与缓存策略。"""
    runtime = _runtime(
        LYRA_MARKETDATA_MODE="delayed",
        **ALPACA_CREDS,
        **MARKETDATA_CREDS,
    )
    providers = {source.id: source for source in runtime.provider_options}

    assert providers["alpaca"].configured is True
    assert providers["alpaca"].asset_types == ("stock", "option")
    assert providers["alpaca"].timing_kind == "delayed"
    assert providers["alpaca"].delay_minutes == 16
    assert providers["alpaca"].cache_ttl_seconds == 900
    assert providers["marketdata"].configured is True
    assert providers["marketdata"].asset_types == ("stock", "option")
    assert providers["marketdata"].timing_kind == "delayed"
    assert providers["marketdata"].delay_minutes == 15
    assert providers["marketdata"].cache_ttl_seconds == 900


def test_persisted_routes_initialize_runtime_without_revision_rollback() -> None:
    """数据库设置覆盖启动默认值，较旧读取不能回退运行时修订号。"""
    from lyra.domain.market_data.types import StockInstrument

    runtime = _runtime()

    initialized = runtime.initialize_settings(
        {"stock": ("marketdata",), "option": ("alpaca",)}, 4
    )
    stale_read = runtime.initialize_settings(
        {"stock": (), "option": ()}, 3
    )

    assert initialized.revision == 4
    assert stale_read == initialized
    assert runtime.settings_snapshot() == initialized
    assert runtime.service.get_quote(StockInstrument(symbol="INTC")).issue == "not_configured"


def test_providers_are_registered_by_credentials_not_routes() -> None:
    """**凭据齐全就注册，与是否被路由选中无关。**

    界面需要展示"已配置但未启用"的来源。
    """
    runtime = _runtime(**ALPACA_CREDS)

    assert runtime.provider_ids == ("alpaca",)
    # 但没有路由。
    assert runtime.default_routes["stock"] == ()


def test_both_providers_can_be_registered() -> None:
    """两个来源都配置时都注册，顺序与配置顺序一致。"""
    runtime = _runtime(**ALPACA_CREDS, **MARKETDATA_CREDS)

    assert runtime.provider_ids == ("alpaca", "marketdata")


def test_routes_are_taken_from_configuration() -> None:
    """路由来自配置，且主子顺序保留。"""
    runtime = _runtime(
        LYRA_MARKET_STOCK_PROVIDERS="marketdata,alpaca",
        LYRA_MARKET_OPTION_PROVIDERS="alpaca",
        **ALPACA_CREDS, **MARKETDATA_CREDS,
    )

    assert runtime.default_routes["stock"] == ("marketdata", "alpaca")
    assert runtime.default_routes["option"] == ("alpaca",)


def test_option_routes_are_independent_from_stock() -> None:
    """正股与期权路由互不影响。"""
    runtime = _runtime(
        LYRA_MARKET_OPTION_PROVIDERS="marketdata", **ALPACA_CREDS, **MARKETDATA_CREDS
    )

    assert runtime.default_routes["stock"] == ()
    assert runtime.default_routes["option"] == ("marketdata",)


def test_historical_mode_gets_a_24_hour_cache() -> None:
    """**historical 的缓存 TTL 是 24 小时**：收盘价当天不会变。"""
    runtime = _runtime(
        LYRA_MARKETDATA_MODE="historical", **MARKETDATA_CREDS
    )

    assert runtime.service._policies["marketdata"].cache_ttl_ms == (
        MARKETDATA_HISTORICAL_CACHE_TTL_MS
    )


def test_delayed_mode_gets_a_15_minute_cache() -> None:
    """delayed 的缓存 TTL 是 15 分钟。"""
    runtime = _runtime(LYRA_MARKETDATA_MODE="delayed", **MARKETDATA_CREDS)

    assert runtime.service._policies["marketdata"].cache_ttl_ms == (
        MARKETDATA_DELAYED_CACHE_TTL_MS
    )


def test_alpaca_policy_has_no_daily_cap() -> None:
    """Alpaca 策略不设每日上限（套餐额度由其自己管理）。"""
    runtime = _runtime(**ALPACA_CREDS)
    policy = runtime.service._policies["alpaca"]

    assert policy.cache_ttl_ms == ALPACA_CACHE_TTL_MS
    assert policy.max_requests_per_day is None


def test_marketdata_policy_uses_the_configured_daily_limit() -> None:
    """marketdata 的每日上限来自配置。"""
    runtime = _runtime(
        LYRA_MARKETDATA_DAILY_REQUEST_LIMIT="42", **MARKETDATA_CREDS
    )

    assert runtime.service._policies["marketdata"].max_requests_per_day == 42


def test_assembly_does_not_make_any_request() -> None:
    """**装配不发起网络请求**：第一次读取才打上游。"""
    calls: list = []

    def fetch(url, *, headers, timeout):
        calls.append(url)
        raise AssertionError("装配阶段不应发起请求")

    config = load_market_data_config({
        "LYRA_MARKET_STOCK_PROVIDERS": "alpaca", **ALPACA_CREDS
    })
    create_market_data_runtime(config, fetch=fetch, now=lambda: 0)

    assert calls == []


def test_marketdata_timing_follows_the_mode() -> None:
    """timing 是对外声明的一部分：historical → 收盘价，delayed → 延迟 15 分钟。"""
    from lyra.domain.market_data.types import StockInstrument

    captured: dict = {}

    def fetch(url, *, headers, timeout):
        captured["url"] = url
        class Response:
            status = 200
            headers: dict = {}
            def chunks(self):
                import json
                yield json.dumps({
                    "s": "ok", "symbol": ["INTC"], "updated": [1760000000],
                    "bid": [12.3], "ask": [12.4], "last": [12.35],
                }).encode()
        return Response()

    for mode, expected in (("historical", "mode=historical"), ("delayed", "mode=delayed")):
        runtime = create_market_data_runtime(
            load_market_data_config({
                "LYRA_MARKET_STOCK_PROVIDERS": "marketdata",
                "LYRA_MARKETDATA_MODE": mode, **MARKETDATA_CREDS,
            }),
            fetch=fetch, now=lambda: 1_760_000_000_000,
        )
        runtime.service.get_quote(StockInstrument(symbol="INTC"))
        assert expected in captured["url"], mode
        # 清掉缓存以免影响下一轮。
        runtime.service._cache.clear()


def test_configured_timeout_reaches_the_transport() -> None:
    """**服务级超时必须真的传到 provider。**

    以前 ``config.timeout_ms`` 只存进运行时 dataclass，两个 provider 调用传输层时都
    不传超时——传输层于是退回自己的默认值（10s），配置形同虚设。
    """
    from lyra.domain.market_data.types import StockInstrument

    seen: list[float] = []

    def fetch(url, *, headers, timeout):
        seen.append(timeout)
        raise RuntimeError("传输失败：这里只关心超时参数")

    config = load_market_data_config({
        "LYRA_MARKET_STOCK_PROVIDERS": "marketdata,alpaca",
        "LYRA_MARKET_TIMEOUT_MS": "7500",
        **ALPACA_CREDS, **MARKETDATA_CREDS,
    })
    runtime = create_market_data_runtime(
        config, fetch=fetch, now=lambda: 1_760_000_000_000
    )

    runtime.service.get_quote(StockInstrument(symbol="INTC"))

    # 两个来源都拿到了配置值（秒）。
    assert seen == [7.5, 7.5]


def test_concurrent_reads_share_one_refresh_and_one_budget_charge() -> None:
    """并发读取同一合约时只刷新一次，不重复消耗来源预算。"""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime
    from threading import Event

    from lyra.application.market_budget import MarketProviderPolicy
    from lyra.application.market_data_service import MarketDataService, ProviderQuote
    from lyra.domain.market_data.types import (
        MarketPricePoint,
        MarketQuote,
        MarketTiming,
        StockInstrument,
    )

    now = 1_800_000_000_000
    as_of = (
        datetime.fromtimestamp(now / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    entered_provider = Event()
    release_provider = Event()
    calls: list[int] = []

    def provider(instrument, *, now_ms):
        calls.append(now_ms)
        entered_provider.set()
        assert release_provider.wait(timeout=2)
        return ProviderQuote(
            status="available",
            quote=MarketQuote(
                instrument_key="US:stock:INTC",
                provider_id="fixture",
                feed="deterministic fixture",
                currency="USD",
                bid=MarketPricePoint(value="10", as_of=as_of),
                ask=MarketPricePoint(value="10.2", as_of=as_of),
                last=None,
                close=None,
                timing=MarketTiming(kind="delayed", delay_minutes=15),
                fetched_at=as_of,
            ),
        )

    service = MarketDataService(
        providers={"fixture": provider},
        routes={"stock": ("fixture",), "option": ()},
        policies={
            "fixture": MarketProviderPolicy(
                cache_ttl_ms=900_000,
                max_requests_per_minute=1,
                max_requests_per_day=None,
            )
        },
        now=lambda: now,
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        reads = [
            executor.submit(service.get_quote, StockInstrument(symbol="INTC"))
            for _ in range(8)
        ]
        assert entered_provider.wait(timeout=2)
        release_provider.set()
        results = [read.result(timeout=2) for read in reads]

    assert len(calls) == 1
    assert len(service._budgets["fixture"].minute_calls) == 1
    assert all(result.status == "available" for result in results)
    assert sum(result.cached for result in results) == 7
