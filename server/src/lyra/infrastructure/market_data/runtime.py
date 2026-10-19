"""行情运行时装配。

镜像 TypeScript 版 ``market-data-runtime.ts`` 的装配部分。

**运行时只持有部署级共享资源**：来源函数（由供应商凭据决定）、公开报价缓存与
供应商预算都挂在同一个 :class:`MarketDataService` 实例上。用户的来源路由是**用户
设置**，每次请求按 ``user_id`` 从数据库读快照、显式传给报价调用。

运行时**不再保存**"当前路由/当前修订号"。把某个用户的设置装进运行时实例，就是
多用户下"谁最后保存谁覆盖所有人"的成因——同一进程里所有请求共享同一个运行时，
任何一次保存都会改写所有人的报价来源（计划 §4.4 点名禁止）。

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

import duckdb

from ...application.market_budget import MarketProviderPolicy
from ...application.market_data_service import MarketDataService, ProviderQuote
from ...domain.market_data.history import MarketHistoryResult
from ...domain.market_data.stock_summary import MarketStockSummary
from ...domain.market_data.settings import MarketProviderOption, MarketRoutes
from ...domain.market_data.types import MarketTiming
from ..db.market_settings_repository import load_market_settings
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
class MarketDataRuntimeSettings:
    """一次一致读取的、**属于某个用户**的已生效路由与修订号。

    @param routes: 该用户当前生效的来源顺序（顺序即主备）。
    @param revision: 与 ``routes`` 同一行的修订号；从未保存过为 0。
    @param updated_at: 最近一次保存时间；从未保存过为 ``None``。
    """

    routes: MarketRoutes
    revision: int
    updated_at: str | None = None


@dataclass
class MarketDataRuntime:
    """部署级共享的行情运行时。

    **只保存与用户无关的装配结果**：共享报价服务（缓存与预算）、部署配置的默认
    路由、来源目录、超时。每个用户的路由与修订号由 :meth:`settings_snapshot` 在
    请求内按 ``user_id`` 读取，运行时不留"当前值"。

    @param service: 部署级共享的报价服务；**不得按用户复制**——那会击穿公开报价
        缓存复用与供应商全局预算（计划 §4.4）。
    @param provider_ids: 凭据齐全的来源 id，供选路与限流使用。
    @param default_routes: 部署配置的路由；某个用户从未保存过设置时使用。
    @param timeout_ms: 单次上游请求超时。
    @param provider_options: 来源目录（含已知但未配置的来源）。
    """

    service: MarketDataService
    provider_ids: tuple[str, ...]
    default_routes: dict[str, tuple[str, ...]]
    timeout_ms: int
    provider_options: tuple[MarketProviderOption, ...]

    def __post_init__(self) -> None:
        """复制部署默认路由，避免调用方改写运行时持有的快照。"""
        self.default_routes = _copy_routes(self.default_routes)

    def settings_snapshot(
        self, connection: duckdb.DuckDBPyConnection, user_id: str
    ) -> MarketDataRuntimeSettings:
        """读取**该用户**当前生效的行情设置快照。

        HTTP 路由与 Agent 工具共用本方法，保证两处看到同一份"当前用户设置 + 共享
        报价服务"。

        **每次调用都读数据库，不做跨请求缓存**：缓存某个用户的设置等价于把它当成
        运行时的"当前值"，多用户下必然互相覆盖（计划 §4.4）。调用方可以把它放进自己
        的短事务，与账本快照一起构成一致读。

        @param connection: 当前数据库连接。
        @param user_id: 当前用户的 UUID 文本；设置行按用户独立。
        @returns 该用户的路由、修订号与保存时间；从未保存过时为部署默认路由与
            revision 0（与"保存了空路由"区分）。
        """
        stored = load_market_settings(connection, user_id)
        if stored is None:
            return MarketDataRuntimeSettings(
                routes=_copy_routes(self.default_routes), revision=0, updated_at=None
            )
        return MarketDataRuntimeSettings(
            routes=_copy_routes(stored.routes),
            revision=stored.revision,
            updated_at=stored.updated_at,
        )


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
    history_providers: dict[str, Callable[..., MarketHistoryResult]] = {}
    stock_summary_providers: dict[str, Callable[..., MarketStockSummary]] = {}
    policies: dict[str, MarketProviderPolicy] = {}

    if config.alpaca_api_key is not None and config.alpaca_secret_key is not None:
        alpaca = AlpacaMarketDataProvider(
            api_key=config.alpaca_api_key, secret_key=config.alpaca_secret_key,
            fetch=fetch, timeout_ms=config.timeout_ms,
        )
        providers[alpaca.id] = alpaca.fetch_quote
        history_providers[alpaca.id] = alpaca.fetch_history
        stock_summary_providers[alpaca.id] = alpaca.fetch_stock_summary
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
            api_token=config.marketdata_api_token, timing=timing, fetch=fetch,
            timeout_ms=config.timeout_ms,
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

    # 部署配置的默认路由：只在"该用户从未保存过设置"时生效，属于部署配置而非用户
    # 设置。它同时作为服务实例的装配默认值，仅 :meth:`MarketDataService.get_quote` 会
    # 使用；按用户读行情一律走 ``read_quote(instrument, route)`` 显式传路由。
    routes = {
        "stock": config.stock_providers,
        "option": config.option_providers,
    }
    provider_options = (
        MarketProviderOption(
            id="alpaca",
            display_name="Alpaca",
            asset_types=("stock", "option"),
            configured="alpaca" in providers,
            timing_kind="delayed",
            delay_minutes=16,
            cache_ttl_seconds=ALPACA_CACHE_TTL_MS // 1000,
        ),
        MarketProviderOption(
            id="marketdata",
            display_name="MarketData.app",
            asset_types=("stock", "option"),
            configured="marketdata" in providers,
            timing_kind="end_of_day" if config.marketdata_mode == "historical" else "delayed",
            delay_minutes=(
                None if config.marketdata_mode == "historical" else MARKETDATA_DELAY_MINUTES
            ),
            cache_ttl_seconds=(
                MARKETDATA_HISTORICAL_CACHE_TTL_MS
                if config.marketdata_mode == "historical"
                else MARKETDATA_DELAYED_CACHE_TTL_MS
            ) // 1000,
        ),
    )
    service = MarketDataService(
        providers=providers,
        history_providers=history_providers,
        stock_summary_providers=stock_summary_providers,
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
        provider_options=provider_options,
    )


def _copy_routes(routes: MarketRoutes) -> dict[str, tuple[str, ...]]:
    """复制两个资产类别的路由，避免调用方修改运行时快照。"""
    return {
        "stock": tuple(routes.get("stock", ())),
        "option": tuple(routes.get("option", ())),
    }
