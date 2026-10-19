"""行情来源配置。

镜像 TypeScript 版 ``market-data-runtime.ts`` 的环境解析部分。

两类配置错误在这里被拦住：

1. **被选中的来源必须有凭据**——缺凭据不是"过一会儿再试"，而是**每次请求都必然
   失败**；让服务带着这种配置启动只会把错误推迟到用户点击时。
2. **只给一半凭据也算错误**。Alpaca 需要 key + secret 一对；只填一个说明操作者本
   打算配置它却填漏了——即使当前没被路由选中，也应该在启动时失败。

秘钥的字符集限制（禁 CR/LF）是**请求头注入防线**：换行会把后续内容变成新的请求头。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

ProviderId = Literal["alpaca", "marketdata"]
MarketDataMode = Literal["historical", "delayed"]

PROVIDER_IDS: tuple[ProviderId, ...] = ("alpaca", "marketdata")

DEFAULT_MARKETDATA_MODE: MarketDataMode = "historical"
DEFAULT_DAILY_REQUEST_LIMIT = 80
DEFAULT_TIMEOUT_MS = 8_000
MIN_TIMEOUT_MS, MAX_TIMEOUT_MS = 1_000, 15_000
MIN_DAILY_LIMIT, MAX_DAILY_LIMIT = 1, 100_000
MAX_SECRET_LENGTH = 8_192

# 换行会把后续内容变成新的请求头。
_FORBIDDEN_IN_SECRET = re.compile(r"[\r\n]")


@dataclass(frozen=True)
class MarketDataConfig:
    """已校验的行情配置。"""

    stock_providers: tuple[ProviderId, ...]
    option_providers: tuple[ProviderId, ...]
    alpaca_api_key: str | None
    alpaca_secret_key: str | None
    marketdata_api_token: str | None
    marketdata_mode: MarketDataMode
    daily_request_limit: int
    timeout_ms: int


def parse_provider_route(raw: str | None) -> tuple[ProviderId, ...]:
    """解析逗号分隔的来源列表。

    @param raw: 环境变量原文；``None`` 或 ``"none"`` 表示不启用。
    @returns 来源列表（保序、去空白）。
    @raises ValueError: 含重复或未知来源。
    """
    if raw is None:
        return ()
    trimmed = raw.strip()
    if trimmed == "" or trimmed == "none":
        return ()
    ids = [part.strip() for part in trimmed.split(",")]
    if len(set(ids)) != len(ids):
        # 重复没有意义，而且通常说明配置里粘了两遍。
        raise ValueError("行情来源路由包含重复项")
    for provider in ids:
        if provider not in PROVIDER_IDS:
            raise ValueError(f"未知的行情来源: {provider}")
    return tuple(ids)  # type: ignore[return-value]


def load_market_data_config(
    environment: Mapping[str, str | None],
) -> MarketDataConfig:
    """读取并交叉校验行情配置。

    @param environment: 环境变量映射。
    @returns 已校验配置。
    @raises ValueError: 路由非法、凭据缺失或含控制字符、数值越界。
    """
    stock = parse_provider_route(environment.get("LYRA_MARKET_STOCK_PROVIDERS"))
    option = parse_provider_route(environment.get("LYRA_MARKET_OPTION_PROVIDERS"))

    alpaca_key = _optional_secret(environment.get("LYRA_ALPACA_API_KEY"), "LYRA_ALPACA_API_KEY")
    alpaca_secret = _optional_secret(
        environment.get("LYRA_ALPACA_SECRET_KEY"), "LYRA_ALPACA_SECRET_KEY"
    )
    token = _optional_secret(
        environment.get("LYRA_MARKETDATA_API_TOKEN"), "LYRA_MARKETDATA_API_TOKEN"
    )

    routes = (*stock, *option)
    # 只给一半凭据同样算错误：操作者显然打算配置它，只是填漏了。
    if "alpaca" in routes or (alpaca_key is None) != (alpaca_secret is None):
        if alpaca_key is None or alpaca_secret is None:
            raise ValueError("选择 Alpaca 来源时必须同时配置 API key 与 secret")
    if "marketdata" in routes and token is None:
        raise ValueError("选择 MarketData 来源时必须配置 API token")

    mode = environment.get("LYRA_MARKETDATA_MODE")
    if mode is not None and mode.strip() != "":
        normalized = mode.strip()
        if normalized not in ("historical", "delayed"):
            raise ValueError("LYRA_MARKETDATA_MODE 只能是 historical 或 delayed")
        resolved_mode: MarketDataMode = normalized  # type: ignore[assignment]
    else:
        resolved_mode = DEFAULT_MARKETDATA_MODE

    return MarketDataConfig(
        stock_providers=stock,
        option_providers=option,
        alpaca_api_key=alpaca_key,
        alpaca_secret_key=alpaca_secret,
        marketdata_api_token=token,
        marketdata_mode=resolved_mode,
        daily_request_limit=_bounded(
            environment.get("LYRA_MARKETDATA_DAILY_REQUEST_LIMIT"),
            DEFAULT_DAILY_REQUEST_LIMIT, MIN_DAILY_LIMIT, MAX_DAILY_LIMIT,
            "LYRA_MARKETDATA_DAILY_REQUEST_LIMIT",
        ),
        timeout_ms=_bounded(
            environment.get("LYRA_MARKET_TIMEOUT_MS"),
            DEFAULT_TIMEOUT_MS, MIN_TIMEOUT_MS, MAX_TIMEOUT_MS, "LYRA_MARKET_TIMEOUT_MS",
        ),
    )


def _optional_secret(raw: str | None, name: str) -> str | None:
    """读取可选秘钥并校验。

    @raises ValueError: 过长或含换行。
    """
    if raw is None:
        return None
    trimmed = raw.strip()
    if trimmed == "":
        return None
    if len(trimmed) > MAX_SECRET_LENGTH:
        raise ValueError(f"{name} 过长")
    if _FORBIDDEN_IN_SECRET.search(trimmed):
        # **换行会把后续内容变成新的请求头**，即请求头注入。
        raise ValueError(f"{name} 不能包含换行")
    return trimmed


def _bounded(raw: str | None, default: int, low: int, high: int, name: str) -> int:
    """读取有界整数。

    @raises ValueError: 非整数或越界。
    """
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise ValueError(f"{name} 必须是整数") from error
    if not low <= value <= high:
        raise ValueError(f"{name} 必须在 {low} 到 {high} 之间")
    return value
