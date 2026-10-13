"""行情路由设置。

镜像 TypeScript 版 ``domain/market-data/settings.ts``。

一条核心区分：**``routes`` 与 ``effectiveRoutes`` 不是一回事**。

- ``routes`` 是用户选定的顺序；
- ``effectiveRoutes`` 是**实际可用的**顺序——把未配置凭据的来源剔除后的结果。

界面必须展示后者，否则用户会看到"已选中 alpaca"而实际每次请求都回退到备来源，
却不知道原因。这是最容易让用户困惑的一类配置问题。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MarketAssetType = Literal["stock", "option"]
ASSET_TYPES: tuple[MarketAssetType, ...] = ("stock", "option")

MarketRoutes = dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class MarketProviderOption:
    """一个可供选择的来源。"""

    id: str
    display_name: str
    asset_types: tuple[MarketAssetType, ...]
    # 凭据是否齐全。未配置的来源可以被选中，但不会生效。
    configured: bool
    timing_kind: str
    delay_minutes: int | None
    cache_ttl_seconds: int


@dataclass(frozen=True)
class MarketSettingsView:
    """公开的行情设置。"""

    revision: int
    routes: MarketRoutes
    effective_routes: MarketRoutes
    providers: tuple[MarketProviderOption, ...]
    updated_at: str | None


def normalize_routes(routes: dict[str, list[str]] | None) -> MarketRoutes:
    """规范化路由设置。

    @param routes: 原始路由；只识别 ``stock`` / ``option``。
    @returns 规范化后的路由；缺失的资产类型为空元组。
    @raises ValueError: 来源 id 非法或重复。
    """
    resolved: MarketRoutes = {asset_type: () for asset_type in ASSET_TYPES}
    if routes is None:
        return resolved
    for asset_type, providers in routes.items():
        if asset_type not in ASSET_TYPES:
            raise ValueError(f"未知的资产类型: {asset_type}")
        if providers is None:
            continue
        cleaned: list[str] = []
        for provider in providers:
            if not isinstance(provider, str) or provider.strip() == "":
                raise ValueError("行情来源 id 不能为空")
            name = provider.strip()
            if name in cleaned:
                # 重复没有意义，而且通常说明请求里粘了两遍。
                raise ValueError(f"行情来源重复: {name}")
            cleaned.append(name)
        resolved[asset_type] = tuple(cleaned)
    return resolved


def effective_routes(
    routes: MarketRoutes, configured_providers: frozenset[str]
) -> MarketRoutes:
    """计算实际生效的路由。

    **剔除未配置凭据的来源**：保留它们会让设置页面显示一个永远不会被使用的顺序，
    而每次请求都静默回退到备来源。

    @param routes: 用户设定的路由。
    @param configured_providers: 凭据齐全的来源。
    @returns 实际生效的路由。
    """
    return {
        asset_type: tuple(
            provider for provider in providers if provider in configured_providers
        )
        for asset_type, providers in routes.items()
    }
