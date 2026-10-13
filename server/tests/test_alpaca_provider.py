"""Alpaca 适配器测试。

三条校验各防一种静默错误：

1. **符号回显**——防止拿另一个合约的价格来定价；
2. **期权 bar 必须分钟对齐且已走完**——否则把"刚开始的那一分钟"当成收盘价；
3. **零成交 bar 视为无数据**——否则给一个没交易的合约凭空赋值。
"""

from __future__ import annotations

import json

import pytest

from lyra.domain.market_data.types import OptionInstrument, StockInstrument
from lyra.infrastructure.market_data.alpaca_provider import (
    AlpacaMarketDataProvider,
    QuoteAvailable,
    QuoteUnavailable,
)

# 让窗口计算可控：now = 1_760_000_000_000（2025-10-09T08:53:20Z）
NOW_MS = 1_760_000_000_000
CUTOFF = NOW_MS - 16 * 60_000  # 08:37:20Z
STOCK_TS = "2025-10-09T08:30:00Z"
# 期权 bar 必须分钟对齐且 bar 结束 ≤ cutoff：08:30:00 + 1min = 08:31 ≤ 08:37:20 ✓
OPTION_TS = "2025-10-09T08:30:00Z"


class FakeResponse:
    """可脚本化的响应。"""

    def __init__(self, *, status: int = 200, payload=None) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self._body = json.dumps(payload).encode()

    def chunks(self):
        yield self._body


def _provider(*, payload=None, status: int = 200, api_key="key", secret="secret"):
    def fetch(url, *, headers, timeout):
        fetch.last_url = url  # type: ignore[attr-defined]
        fetch.last_headers = headers  # type: ignore[attr-defined]
        return FakeResponse(status=status, payload=payload)

    return (
        AlpacaMarketDataProvider(api_key=api_key, secret_key=secret, fetch=fetch),
        fetch,
    )


def _stock_payload(**overrides):
    base = {"symbol": "INTC", "quotes": [{"t": STOCK_TS, "bp": 12.3, "ap": 12.4}]}
    base.update(overrides)
    return base


def _option_payload(symbol="INTC270319C00100000", **row):
    entry = {"t": OPTION_TS, "c": 2.5, "v": 100}
    entry.update(row)
    return {"bars": {symbol: [entry]}}


# --------------------------------------------------------------------------
# 正股
# --------------------------------------------------------------------------


def test_stock_quote_uses_bid_and_ask() -> None:
    """正股报价取 bp / ap，且没有收盘价。"""
    provider, _ = _provider(payload=_stock_payload())

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteAvailable)
    quote = result.quote
    assert quote.bid is not None and quote.bid.value == "12.3"
    assert quote.ask is not None and quote.ask.value == "12.4"
    assert quote.close is None
    assert quote.last is None
    assert quote.timing.kind == "delayed"


def test_stock_request_uses_sip_feed_and_lookback_window() -> None:
    """正股请求带 sip feed、desc 排序与 7 天回看窗口。"""
    provider, fetch = _provider(payload=_stock_payload())

    provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    url = fetch.last_url  # type: ignore[attr-defined]
    assert "/v2/stocks/INTC/quotes" in url
    assert "feed=sip" in url and "sort=desc" in url and "limit=1" in url
    # 窗口上界是 cutoff（不向下取整）。
    assert "end=2025-10-09T08%3A37%3A20.000Z" in url or "end=2025-10-09T08:37:20.000Z" in url


def test_stock_symbol_mismatch_is_rejected() -> None:
    """**回显合约不符时拒绝。**"""
    provider, _ = _provider(payload=_stock_payload(symbol="AMD"))

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "invalid_response"


def test_stock_null_quotes_means_no_data() -> None:
    """`quotes: null` 是"无数据"，不是错误。"""
    provider, _ = _provider(payload=_stock_payload(quotes=None))

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "no_data"


def test_stock_all_prices_absent_is_no_data() -> None:
    """bp/ap 都为空 → no_data。"""
    provider, _ = _provider(payload=_stock_payload(
        quotes=[{"t": STOCK_TS, "bp": None, "ap": None}]
    ))

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "no_data"


# --------------------------------------------------------------------------
# 期权
# --------------------------------------------------------------------------


def _option() -> OptionInstrument:
    return OptionInstrument(
        underlying="INTC", expiry="2027-03-19", strike="100.00", option_type="C"
    )


def test_option_quote_uses_the_bar_close() -> None:
    """期权报价取 bar 的 c，时点是 bar 收盘时间（起点 + 1 分钟）。"""
    provider, _ = _provider(payload=_option_payload())

    result = provider.fetch_quote(_option(), now_ms=NOW_MS)

    assert isinstance(result, QuoteAvailable)
    quote = result.quote
    assert quote.close is not None
    assert quote.close.value == "2.5"
    assert quote.close.as_of == "2025-10-09T08:31:00.000Z"
    # 该端点不提供 bid/ask。
    assert quote.bid is None and quote.ask is None


def test_option_request_uses_bars_endpoint() -> None:
    """期权走 bars 端点并带 symbols / timeframe。"""
    provider, fetch = _provider(payload=_option_payload())

    provider.fetch_quote(_option(), now_ms=NOW_MS)

    url = fetch.last_url  # type: ignore[attr-defined]
    assert "/v1beta1/options/bars" in url
    assert "symbols=INTC270319C00100000" in url
    assert "timeframe=1Min" in url


def test_option_end_is_floored_to_the_minute() -> None:
    """**期权的窗口上界向下取整到分钟再减 1ms。**

    否则会把"刚开始、尚未走完"的那一分钟 bar 算进来。
    """
    provider, fetch = _provider(payload=_option_payload())

    provider.fetch_quote(_option(), now_ms=NOW_MS)

    url = fetch.last_url  # type: ignore[attr-defined]
    # cutoff 08:37:20 → 向下取整 08:37:00 → 减 1ms = 08:36:59.999Z
    assert "08%3A36%3A59.999Z" in url or "08:36:59.999Z" in url


def test_option_bar_must_be_minute_aligned() -> None:
    """**未分钟对齐的 bar 必须拒绝。**"""
    provider, _ = _provider(payload=_option_payload(t="2025-10-09T08:30:30Z"))

    result = provider.fetch_quote(_option(), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "invalid_response"


def test_option_bar_must_be_complete() -> None:
    """**尚未走完的 bar 必须拒绝**：它的 c 不是收盘价。"""
    # 08:37:00 对齐，但 08:37:00 + 1min = 08:38 > cutoff 08:37:20
    provider, _ = _provider(payload=_option_payload(t="2025-10-09T08:37:00Z"))

    result = provider.fetch_quote(_option(), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "invalid_response"


@pytest.mark.parametrize("volume", [0])
def test_zero_volume_option_is_no_data(volume: int) -> None:
    """**零成交的 bar 没有可定价的价格。**

    当成价格会凭空给一个没交易的合约赋值。
    """
    provider, _ = _provider(payload=_option_payload(v=volume))

    result = provider.fetch_quote(_option(), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "no_data"


@pytest.mark.parametrize("volume", [-1, 1.5, "100", True])
def test_invalid_volume_is_rejected(volume: object) -> None:
    """成交量必须是合法非负整数。"""
    provider, _ = _provider(payload=_option_payload(v=volume))

    result = provider.fetch_quote(_option(), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "invalid_response"


def test_option_symbol_mismatch_is_rejected() -> None:
    """回显的期权符号不符时拒绝。"""
    provider, _ = _provider(payload=_option_payload(symbol="AMD270319C00100000"))

    result = provider.fetch_quote(_option(), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "invalid_response"


def test_multiple_option_symbols_are_rejected() -> None:
    """响应里出现多个合约时拒绝。"""
    provider, _ = _provider(payload={"bars": {
        "INTC270319C00100000": [{"t": OPTION_TS, "c": 2.5, "v": 100}],
        "AMD270319C00100000": [{"t": OPTION_TS, "c": 1.0, "v": 50}],
    }})

    result = provider.fetch_quote(_option(), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "invalid_response"


def test_option_null_bars_means_no_data() -> None:
    """`bars: null` 是"无数据"。"""
    provider, _ = _provider(payload={"bars": None})

    result = provider.fetch_quote(_option(), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "no_data"


def test_option_timestamp_outside_window_is_rejected() -> None:
    """窗口外的时间戳必须拒绝——它可能是很旧的缓存。"""
    provider, _ = _provider(payload=_option_payload(t="2025-01-02T08:30:00Z"))

    result = provider.fetch_quote(_option(), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "invalid_response"


# --------------------------------------------------------------------------
# 通用路径
# --------------------------------------------------------------------------


def test_404_is_provider_unavailable() -> None:
    """404 表示来源不可用。"""
    provider, _ = _provider(status=404, payload={})

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "provider_unavailable"


@pytest.mark.parametrize(
    ("api_key", "secret"),
    [
        ("", "secret"),
        ("key", "  "),
        # **内部**控制字符：构造函数会 strip 掉首尾空白（与 TS 的 trim 一致），
        # 因此尾部换行会被去掉后变成合法凭据，只有内部的控制字符才真的破坏请求头。
        ("ke\ny", "s"),
        ("key", "se\x00cret"),
    ],
)
def test_invalid_credentials_short_circuit(api_key: str, secret: str) -> None:
    """凭据缺失或含内部控制字符时不发请求。"""
    provider, fetch = _provider(payload=_stock_payload(), api_key=api_key, secret=secret)

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "not_configured"
    assert not hasattr(fetch, "last_url")


def test_unsupported_instrument_short_circuits() -> None:
    """非标准合约在翻译阶段被拒，不发请求。"""
    provider, fetch = _provider(payload=_stock_payload())

    result = provider.fetch_quote(StockInstrument(symbol="lowercase"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "unsupported_instrument"
    assert not hasattr(fetch, "last_url")


def test_credentials_are_sent_in_headers_only() -> None:
    """**密钥只出现在请求头**，不进 URL。"""
    provider, fetch = _provider(payload=_stock_payload())

    provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert fetch.last_headers["APCA-API-KEY-ID"] == "key"  # type: ignore[attr-defined]
    assert fetch.last_headers["APCA-API-SECRET-KEY"] == "secret"  # type: ignore[attr-defined]
    assert "secret" not in fetch.last_url  # type: ignore[attr-defined]
