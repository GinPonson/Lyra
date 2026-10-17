"""Alpaca 行情适配器。

镜像 TypeScript 版 ``infrastructure/market-data/alpaca-provider.ts``。

**两类合约走完全不同的端点与字段**：

| | 正股 | 期权 |
| --- | --- | --- |
| 端点 | ``/v2/stocks/{symbol}/quotes`` | ``/v1beta1/options/bars`` |
| 字段 | ``bp`` / ``ap`` | ``c``（bar 收盘） |
| 时效窗口 | ``end = cutoff`` | ``end`` **向下取整到分钟再减 1ms** |

期权那条 ``- 1ms`` 是关键：不然会把"刚刚开始、尚未走完"的那一分钟 bar 算进来，
而它的 ``c`` 只是那一瞬间的价格，不是收盘价。

三处校验，每处都防一种静默错误：

1. **符号回显**——防止拿另一个合约的价格来定价；
2. **bar 必须分钟对齐且已走完**（``asOf % 60000 == 0`` 且 ``asOf + 60000 <= cutoff``）；
3. **成交量必须是合法非负整数，且为 0 时视为无数据**——零成交的 bar 没有可定价的
   价格，当成价格会凭空给一个没交易的合约赋值。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import Any, Literal

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
    market_timestamp,
    price_point,
    provider_instrument,
    valid_credential,
)

DELAY_MILLISECONDS = 16 * 60_000
LOOKBACK_MILLISECONDS = 7 * 24 * 60 * 60_000
STOCK_QUOTES_URL = "https://data.alpaca.markets/v2/stocks"
OPTION_BARS_URL = "https://data.alpaca.markets/v1beta1/options/bars"

# Alpaca 的报价是 SIP（全市场）口径的延迟行情。
ALPACA_TIMING = MarketTiming(kind="delayed", delay_minutes=16)


@dataclass(frozen=True)
class QuoteAvailable:
    """取到了报价。"""

    status: Literal["available"]
    quote: MarketQuote


@dataclass(frozen=True)
class QuoteUnavailable:
    """没取到报价，带稳定原因。"""

    status: Literal["unavailable"]
    reason: MarketQuoteIssue


ProviderQuoteResult = QuoteAvailable | QuoteUnavailable


class AlpacaMarketDataProvider:
    """Alpaca 适配器。

    @param api_key: 密钥 ID。
    @param secret_key: 密钥。
    @param fetch: 传输层。
    @param provider_id: 来源标识。
    @param timeout_ms: 单次请求超时；由运行时从 ``config.timeout_ms`` 注入。
    """

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        fetch: MarketDataFetch,
        provider_id: str = "alpaca",
        timeout_ms: int = MARKET_DATA_TIMEOUT_MILLISECONDS,
    ) -> None:
        self._api_key = api_key.strip()
        self._secret_key = secret_key.strip()
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
        if not valid_credential(self._api_key) or not valid_credential(self._secret_key):
            return QuoteUnavailable(status="unavailable", reason="not_configured")

        cutoff = now_ms - DELAY_MILLISECONDS
        start = cutoff - LOOKBACK_MILLISECONDS
        is_stock = isinstance(instrument, StockInstrument)
        if is_stock:
            end = cutoff
        else:
            # 向下取整到分钟再减 1ms：避免把尚未走完的那一分钟 bar 当成收盘价。
            end = (cutoff // 60_000) * 60_000 - 1

        if is_stock:
            url = (
                f"{STOCK_QUOTES_URL}/{_encode(identity.symbol)}/quotes"
                f"?start={_iso(start)}&end={_iso(end)}&sort=desc&limit=1&feed=sip"
            )
        else:
            url = (
                f"{OPTION_BARS_URL}?start={_iso(start)}&end={_iso(end)}"
                f"&sort=desc&limit=1&symbols={_encode(identity.symbol)}&timeframe=1Min"
            )

        response = request_market_json(
            url=url,
            headers={
                "APCA-API-KEY-ID": self._api_key,
                "APCA-API-SECRET-KEY": self._secret_key,
            },
            fetch=self._fetch,
            # 服务级超时必须真的传到传输层，否则配置了也不生效（默认 10s 会盖过它）。
            timeout_ms=self._timeout_ms,
        )
        if not response.ok:
            return QuoteUnavailable(status="unavailable", reason=response.reason)
        if response.status == 404:
            return QuoteUnavailable(status="unavailable", reason="provider_unavailable")

        try:
            payload = response.body
            if not is_record(payload):
                raise ValueError("Invalid Alpaca payload")
            rows = (
                _stock_rows(payload, identity.symbol)
                if is_stock
                else _option_rows(payload, identity.symbol)
            )
            if len(rows) == 0:
                return QuoteUnavailable(status="unavailable", reason="no_data")
            if len(rows) != 1 or not is_record(rows[0]):
                raise ValueError("Invalid Alpaca rows")
            row = rows[0]
            as_of = market_timestamp(row.get("t"), start, end)

            if not is_stock:
                milliseconds = _parse_millis(as_of)
                # bar 必须分钟对齐且已经走完。
                if milliseconds % 60_000 != 0 or milliseconds + 60_000 > cutoff:
                    raise ValueError("Invalid Alpaca bar interval")
                volume = row.get("v")
                if (
                    isinstance(volume, bool)
                    or not isinstance(volume, int)
                    or volume < 0
                ):
                    raise ValueError("Invalid Alpaca option volume")
                # 零成交的 bar 没有可定价的价格。
                if volume == 0:
                    return QuoteUnavailable(status="unavailable", reason="no_data")

            bid = price_point(row.get("bp"), as_of) if is_stock else None
            ask = price_point(row.get("ap"), as_of) if is_stock else None
            close = (
                None
                if is_stock
                # bar 的收盘时间 = bar 起点 + 1 分钟。
                else price_point(row.get("c"), _iso(_parse_millis(as_of) + 60_000))
            )
            if bid is None and ask is None and close is None:
                return QuoteUnavailable(status="unavailable", reason="no_data")

            return QuoteAvailable(
                status="available",
                quote=MarketQuote(
                    instrument_key=identity.instrument_key,
                    provider_id=self.id,
                    feed="Alpaca SIP delayed quotes",
                    currency="USD",
                    bid=bid,
                    ask=ask,
                    last=None,
                    close=close,
                    timing=ALPACA_TIMING,
                    fetched_at=_iso(now_ms),
                ),
            )
        except Exception:  # noqa: BLE001 - 结构异常折叠为 invalid_response
            return QuoteUnavailable(status="unavailable", reason="invalid_response")


def _stock_rows(payload: dict[str, Any], symbol: str) -> list[Any]:
    """取正股报价行。

    @raises ValueError: 回显合约与请求不符，或字段结构非法。
    """
    if payload.get("symbol") != symbol:
        # 与 MarketData 同理：不核对就可能拿另一个合约的价格来定价。
        raise ValueError("Alpaca stock symbol mismatch")
    quotes = payload.get("quotes")
    if quotes is None:
        return []
    if not isinstance(quotes, list):
        raise ValueError("Invalid Alpaca quotes")
    return quotes


def _option_rows(payload: dict[str, Any], symbol: str) -> list[Any]:
    """取期权 bar 行。

    @raises ValueError: bars 结构非法或回显合约与请求不符。
    """
    bars = payload.get("bars")
    if bars is None:
        return []
    if not isinstance(bars, dict):
        raise ValueError("Invalid Alpaca bars")
    symbols = list(bars)
    if len(symbols) == 0:
        return []
    if len(symbols) != 1 or symbols[0] != symbol:
        raise ValueError("Alpaca option symbol mismatch")
    rows = bars[symbol]
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("Invalid Alpaca option rows")
    return rows


def _parse_millis(value: str) -> int:
    """解析 ISO 时间戳为毫秒。"""
    from .identifiers import _parse_millis as parse

    return parse(value)


def _iso(milliseconds: int) -> str:
    """把毫秒时间戳渲染为 ISO 文本。"""
    from datetime import datetime

    return (
        datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _encode(value: str) -> str:
    """URL 编码路径片段。"""
    from urllib.parse import quote

    return quote(value, safe="")
