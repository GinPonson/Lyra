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
from dataclasses import dataclass, field
from threading import RLock

from ...application.market_budget import MarketProviderPolicy
from ...application.market_data_service import MarketDataService, ProviderQuote
from ...domain.market_data.settings import MarketProviderOption, MarketRoutes
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
class MarketDataRuntimeSettings:
    """一次一致读取的已生效路由与持久化修订号。"""

    routes: MarketRoutes
    revision: int


@dataclass
class MarketDataRuntime:
    """装配好的行情运行时及其当前持久化设置。"""

    service: MarketDataService
    # 可选来源 id（凭据齐全的），供运行时选路与限流使用。
    provider_ids: tuple[str, ...]
    # 启动配置路由；数据库中有保存值时由 initialize_settings 覆盖。
    default_routes: dict[str, tuple[str, ...]]
    timeout_ms: int
    # 来源目录包含已知但未配置的来源，供设置页显示其真实不可用状态。
    provider_options: tuple[MarketProviderOption, ...]
    _routes: dict[str, tuple[str, ...]] = field(init=False, repr=False)
    _settings_revision: int = field(default=0, init=False, repr=False)
    _settings_lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        """以启动配置路由初始化服务状态。"""
        self.default_routes = _copy_routes(self.default_routes)
        self._routes = _copy_routes(self.default_routes)

    def initialize_settings(
        self, routes: MarketRoutes, revision: int
    ) -> MarketDataRuntimeSettings:
        """用数据库保存的路由与修订号初始化或更新运行时。

        @param routes: 已校验的持久化路由。
        @param revision: 与路由同一行的修订号。
        @returns 当前实际生效的路由快照；较旧的并发读取不会覆盖较新修订。
        @raises ValueError: 修订号为负数。
        """
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
        ):
            raise ValueError("行情设置 revision 不能为负数")
        resolved = _copy_routes(routes)
        with self._settings_lock:
            # 请求可能先读到旧设置、后于新保存请求到达此处；只允许修订号向前推进。
            if revision >= self._settings_revision:
                self.service.set_routes(resolved)
                self._routes = resolved
                self._settings_revision = revision
            return MarketDataRuntimeSettings(
                routes=_copy_routes(self._routes), revision=self._settings_revision
            )

    def settings_snapshot(self) -> MarketDataRuntimeSettings:
        """返回路由和修订号一致的当前快照。"""
        with self._settings_lock:
            return MarketDataRuntimeSettings(
                routes=_copy_routes(self._routes), revision=self._settings_revision
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
    policies: dict[str, MarketProviderPolicy] = {}

    if config.alpaca_api_key is not None and config.alpaca_secret_key is not None:
        alpaca = AlpacaMarketDataProvider(
            api_key=config.alpaca_api_key, secret_key=config.alpaca_secret_key,
            fetch=fetch, timeout_ms=config.timeout_ms,
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
