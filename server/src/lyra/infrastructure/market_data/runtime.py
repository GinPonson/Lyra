"""行情运行时装配。

镜像 TypeScript 版 ``market-data-runtime.ts`` 的装配部分。

**供应商按凭据注册，与路由无关**：一对齐全的凭据说明操作者配置了这个来源，
即使当前没被路由选中也应当出现在可选列表里（界面需要展示"已配置但未启用"）。

策略按来源不同：

| 来源 | 缓存 TTL | 每分钟 | 每日 |
| --- | --- | --- | --- |
| alpaca | 15 分钟 | 120 | 无上限 |
| marketdata（historical） | **24 小时** | 30 | 配置值 |
| marketdata（delayed） | 15 分钟 | 30 | 配置值 |

historical 的 TTL 是 24 小时：**收盘价在当天不会变**，15 分钟一次纯属浪费额度。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ...application.market_budget import MarketProviderPolicy
from ...application.market_data_service import MarketDataService, ProviderQuote
from ...domain.market_data.types import MarketTiming
from .alpaca_provider import AlpacaMarketDataProvider
from .config import MarketDataConfig
from .http import MarketDataFetch
from .marketdata_provider import MarketDataProvider

ALPACA_CACHE_TTL_MS = 15 * 60_000
ALPACA_REQUESTS_PER_MINUTE = 120
MARKETDATA_HISTORICAL_CACHE_TTL_MS = 86_400_000
MARKETDATA_DELAYED_CACHE_TTL_MS = 15 * 60_000
MARKETDATA_REQUESTS_PER_MINUTE = 30
MARKETDATA_DELAY_MINUTES = 15


@dataclass(frozen=True)
class MarketDataRuntime:
    """装配好的行情运行时。"""

    service: MarketDataService
    # 可选来源 id（已配置凭据的），供界面展示。
    provider_ids: tuple[str, ...]
    default_routes: dict[str, tuple[str, ...]]
    timeout_ms: int


def create_market_data_runtime(
    config: MarketDataConfig,
    *,
    fetch: MarketDataFetch,
    now: Callable[[], int],
    max_cache_entries: int | None = None,
) -> MarketDataRuntime:
    """按配置装配行情运行时。

    **不发起任何网络请求**：装配只构造对象，第一次读取才打上游。

    @param config: 已校验配置。
    @param fetch: 传输层。
    @param now: 当前毫秒时间戳提供者。
    @param max_cache_entries: 缓存条目上限；``None`` 用默认。
    @returns 运行时。
    """
    providers: dict[str, Callable[..., ProviderQuote]] = {}
    policies: dict[str, MarketProviderPolicy] = {}

    if config.alpaca_api_key is not None and config.alpaca_secret_key is not None:
        alpaca = AlpacaMarketDataProvider(
            api_key=config.alpaca_api_key, secret_key=config.alpaca_secret_key, fetch=fetch
        )
        providers[alpaca.id] = alpaca.fetch_quote
        policies[alpaca.id] = MarketProviderPolicy(
            cache_ttl_ms=ALPACA_CACHE_TTL_MS,
            max_requests_per_minute=ALPACA_REQUESTS_PER_MINUTE,
            max_requests_per_day=None,
        )

    if config.marketdata_api_token is not None:
        historical = config.marketdata_mode == "historical"
        # **timing 是对外声明的一部分**：historical 是收盘价口径，delayed 是延迟报价。
        timing = MarketTiming(
            kind="end_of_day" if historical else "delayed",
            delay_minutes=None if historical else MARKETDATA_DELAY_MINUTES,
        )
        marketdata = MarketDataProvider(
            api_token=config.marketdata_api_token, timing=timing, fetch=fetch
        )
        providers[marketdata.id] = marketdata.fetch_quote
        policies[marketdata.id] = MarketProviderPolicy(
            # 收盘价当天不会变，15 分钟一次纯属浪费额度。
            cache_ttl_ms=(
                MARKETDATA_HISTORICAL_CACHE_TTL_MS
                if historical
                else MARKETDATA_DELAYED_CACHE_TTL_MS
            ),
            max_requests_per_minute=MARKETDATA_REQUESTS_PER_MINUTE,
            max_requests_per_day=config.daily_request_limit,
        )

    routes = {
        "stock": config.stock_providers,
        "option": config.option_providers,
    }
    service = MarketDataService(
        providers=providers,
        routes=routes,
        policies=policies,
        now=now,
        **({} if max_cache_entries is None else {"max_cache_entries": max_cache_entries}),
    )
    return MarketDataRuntime(
        service=service,
        provider_ids=tuple(providers),
        default_routes=routes,
        timeout_ms=config.timeout_ms,
    )
