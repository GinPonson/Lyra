"""MarketData.app 行情适配器。

镜像 TypeScript 版 ``infrastructure/market-data/marketdata-provider.ts``。

**最要紧的一条是符号回显校验。** 供应商会在响应里回显它实际查询的合约；
若不核对，一次参数拼错或供应商侧的路由 bug 就会**用一个合约的价格去给另一个合约
定价**——而报价看起来完全正常，没有任何异常。

其余取舍：

- ``mode`` 显式指定 ``delayed`` / ``historical``：付费账号默认返回实时，但我们对外
  声明的是延迟报价，必须请求与实际声明一致的新鲜度；
- **不加 date/from/to**：本契约是"最新可得"，不是历史查询；
- ``bid``/``ask``/``last`` 全空 → ``no_data``，而不是返回一条没有价格的报价。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import Any

from ...domain.market_data.types import (
    MarketInstrument,
    MarketQuote,
    MarketQuoteIssue,
    MarketTiming,
    StockInstrument,
)
from .http import MARKET_DATA_TIMEOUT_MILLISECONDS, MarketDataFetch, request_market_json
from .identifiers import (
    is_record,
    price_point,
    provider_instrument,
    unix_market_timestamp,
    valid_credential,
)

MARKETDATA_BASE_URL = "https://api.marketdata.app/v1"


@dataclass(frozen=True)
class QuoteAvailable:
    """取到了报价。"""

    status: str
    quote: MarketQuote


@dataclass(frozen=True)
class QuoteUnavailable:
    """没取到报价，带稳定原因。"""

    status: str
    reason: MarketQuoteIssue


ProviderQuoteResult = QuoteAvailable | QuoteUnavailable


class MarketDataProvider:
    """MarketData.app 适配器。

    @param api_token: API 令牌。
    @param timing: 本实例声明的报价时效。
    @param fetch: 传输层。
    @param provider_id: 来源标识。
    @param timeout_ms: 单次请求超时；由运行时从 ``config.timeout_ms`` 注入。
    """

    def __init__(
        self,
        *,
        api_token: str,
        timing: MarketTiming,
        fetch: MarketDataFetch,
        provider_id: str = "marketdata",
        timeout_ms: int = MARKET_DATA_TIMEOUT_MILLISECONDS,
    ) -> None:
        self._api_token = api_token
        self._timing = timing
        self._fetch = fetch
        self._timeout_ms = timeout_ms
        self.id = provider_id

    def fetch_quote(self, instrument: MarketInstrument, *, now_ms: int) -> ProviderQuoteResult:
        """取一条报价。

        @param instrument: 内部合约。
        @param now_ms: 当前毫秒时间戳。
        @returns 报价或稳定原因码。
        """
        identity = provider_instrument(instrument)
        if not identity.ok:
            return QuoteUnavailable(status="unavailable", reason=identity.reason)
        if not valid_credential(self._api_token):
            return QuoteUnavailable(status="unavailable", reason="not_configured")

        resource = "stocks" if isinstance(instrument, StockInstrument) else "options"
        # `mode` 必须与对外声明的时效一致：付费账号默认返回实时，而 Lyra 声明的是延迟。
        mode = "historical" if self._timing.kind == "end_of_day" else "delayed"
        url = (
            f"{MARKETDATA_BASE_URL}/{resource}/quotes/"
            f"{_encode(identity.symbol)}/?mode={mode}"
        )
        response = request_market_json(
            url=url,
            headers={"authorization": f"Bearer {self._api_token}"},
            fetch=self._fetch,
            # 服务级超时必须真的传到传输层，否则配置了也不生效（默认 10s 会盖过它）。
            timeout_ms=self._timeout_ms,
        )
        if not response.ok:
            return QuoteUnavailable(status="unavailable", reason=response.reason)
        if response.status == 404:
            # 与 HTTP 客户端层的 404 语义不同：这里 404 表示该合约无报价。
            return QuoteUnavailable(status="unavailable", reason="provider_unavailable")

        payload = response.body
        if is_record(payload) and payload.get("s") == "no_data":
            return QuoteUnavailable(status="unavailable", reason="no_data")

        try:
            if not is_record(payload) or payload.get("s") != "ok":
                raise ValueError("Invalid MarketData status")
            # **符号回显校验**：不核对就可能拿另一个合约的价格来定价。
            symbol_field = "symbol" if isinstance(instrument, StockInstrument) else "optionSymbol"
            symbols = payload.get(symbol_field)
            if (
                not isinstance(symbols, list)
                or len(symbols) != 1
                or symbols[0] != identity.symbol
            ):
                raise ValueError("MarketData symbol mismatch")

            as_of = unix_market_timestamp(_single(payload, "updated"), now_ms)
            bid = price_point(_single(payload, "bid"), as_of)
            ask = price_point(_single(payload, "ask"), as_of)
            last = price_point(_single(payload, "last"), as_of)
            if bid is None and ask is None and last is None:
                return QuoteUnavailable(status="unavailable", reason="no_data")

            return QuoteAvailable(
                status="available",
                quote=MarketQuote(
                    instrument_key=identity.instrument_key,
                    provider_id=self.id,
                    feed=f"MarketData {resource} quotes (account-entitled snapshot)",
                    currency="USD",
                    bid=bid,
                    ask=ask,
                    last=last,
                    # 该端点不提供收盘价。
                    close=None,
                    timing=self._timing,
                    fetched_at=_iso_ms(now_ms),
                ),
            )
        except Exception:  # noqa: BLE001 - 结构异常折叠为 invalid_response
            return QuoteUnavailable(status="unavailable", reason="invalid_response")


def _single(payload: dict[str, Any], column: str) -> Any:
    """取出单元素列的值。

    @raises ValueError: 列缺失、不是数组或长度不为 1——多元素意味着响应里混了别的合约。
    """
    values = payload.get(column)
    if not isinstance(values, list) or len(values) != 1:
        raise ValueError("Invalid MarketData column")
    return values[0]


def _encode(value: str) -> str:
    """URL 编码路径片段。"""
    from urllib.parse import quote

    return quote(value, safe="")


def _iso_ms(milliseconds: int) -> str:
    """把毫秒时间戳渲染为 ISO 文本。"""
    from datetime import datetime

    return (
        datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
