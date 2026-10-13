"""MarketData 适配器测试。

最要紧的一条是 :func:`test_symbol_mismatch_is_rejected`：
**供应商会在响应里回显它实际查询的合约**；不核对就可能用一个合约的价格去给另一个
合约定价——而报价看起来完全正常，没有任何异常。
"""

from __future__ import annotations

import json

from lyra.domain.market_data.types import (
    MarketPricePoint,
    MarketTiming,
    OptionInstrument,
    StockInstrument,
)
from lyra.infrastructure.market_data.marketdata_provider import (
    MarketDataProvider,
    QuoteAvailable,
    QuoteUnavailable,
)

NOW_MS = 1_760_000_000_000
DELAYED = MarketTiming(kind="delayed", delay_minutes=15)


class FakeResponse:
    """可脚本化的响应。"""

    def __init__(self, *, status: int = 200, payload=None, body: bytes | None = None) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self._body = body if body is not None else json.dumps(payload).encode()

    def chunks(self):
        yield self._body


def _provider(*, payload=None, status: int = 200, token: str = "token", timing=DELAYED, body=None):
    def fetch(url, *, headers, timeout):
        fetch.last_url = url  # type: ignore[attr-defined]
        fetch.last_headers = headers  # type: ignore[attr-defined]
        return FakeResponse(status=status, payload=payload, body=body)

    return MarketDataProvider(api_token=token, timing=timing, fetch=fetch), fetch


def _ok_payload(symbol: str = "INTC", **overrides):
    base = {
        "s": "ok",
        "symbol": [symbol],
        "updated": [1_760_000_000],
        "bid": [12.3],
        "ask": [12.4],
        "last": [12.35],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# 成功
# --------------------------------------------------------------------------


def test_returns_a_quote_for_a_stock() -> None:
    """正股报价解析成功。"""
    provider, _ = _provider(payload=_ok_payload())

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteAvailable)
    quote = result.quote
    assert quote.instrument_key == "US:stock:INTC"
    assert quote.provider_id == "marketdata"
    assert quote.currency == "USD"
    assert quote.bid == MarketPricePoint(value="12.3", as_of="2025-10-09T08:53:20.000Z")
    assert quote.ask is not None and quote.ask.value == "12.4"
    assert quote.last is not None and quote.last.value == "12.35"
    # 该端点不提供收盘价。
    assert quote.close is None
    assert quote.timing == DELAYED


def test_requests_delayed_mode_for_delayed_timing() -> None:
    """**mode 必须与对外声明的时效一致。**

    付费账号默认返回实时，而 Lyra 声明的是延迟报价——不一致就是在夸大数据新鲜度。
    """
    provider, fetch = _provider(payload=_ok_payload())

    provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert "mode=delayed" in fetch.last_url  # type: ignore[attr-defined]
    assert "/stocks/quotes/INTC/" in fetch.last_url  # type: ignore[attr-defined]


def test_requests_historical_mode_for_end_of_day() -> None:
    """收盘价口径请求 historical。"""
    provider, fetch = _provider(
        payload=_ok_payload(), timing=MarketTiming(kind="end_of_day", delay_minutes=None)
    )

    provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert "mode=historical" in fetch.last_url  # type: ignore[attr-defined]


def test_options_use_the_occ_symbol_and_option_field() -> None:
    """期权走 options 资源，并用 optionSymbol 回显字段。"""
    provider, fetch = _provider(payload=_ok_payload(
        symbol="INTC270319C00100000",
        optionSymbol=["INTC270319C00100000"],
    ))
    instrument = OptionInstrument(
        underlying="INTC", expiry="2027-03-19", strike="100.00", option_type="C"
    )

    result = provider.fetch_quote(instrument, now_ms=NOW_MS)

    assert isinstance(result, QuoteAvailable)
    assert "/options/quotes/INTC270319C00100000/" in fetch.last_url  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 符号回显（核心安全性质）
# --------------------------------------------------------------------------


def test_symbol_mismatch_is_rejected() -> None:
    """**回显的合约与请求不符时拒绝。**

    否则一次参数拼错或供应商侧的路由 bug 就会用一个合约的价格给另一个合约定价，
    而报价看起来完全正常。
    """
    provider, _ = _provider(payload=_ok_payload(symbol="AMD"))

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "invalid_response"


def test_multiple_echoed_symbols_are_rejected() -> None:
    """回显多个合约同样拒绝——响应里混了别的东西。"""
    provider, _ = _provider(payload=_ok_payload(symbol=["INTC", "AMD"]))

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "invalid_response"


def test_multi_element_column_is_rejected() -> None:
    """列里多于一个值意味着混了别的合约，必须拒绝。"""
    provider, _ = _provider(payload=_ok_payload(bid=[12.3, 99.9]))

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "invalid_response"


# --------------------------------------------------------------------------
# 不可用路径
# --------------------------------------------------------------------------


def test_no_data_status_maps_to_no_data() -> None:
    """`s = "no_data"` 映射为 no_data。"""
    provider, _ = _provider(payload={"s": "no_data"})

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "no_data"


def test_404_is_provider_unavailable_here() -> None:
    """这里的 404 表示来源不可用（与 HTTP 客户端层的语义不同）。"""
    provider, _ = _provider(status=404, payload={})

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "provider_unavailable"


def test_all_prices_absent_maps_to_no_data() -> None:
    """`bid`/`ask`/`last` 全空 → no_data，而不是返回一条没有价格的报价。"""
    provider, _ = _provider(payload=_ok_payload(bid=[None], ask=[None], last=[None]))

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "no_data"


def test_zero_prices_count_as_absent() -> None:
    """零报价是"没有可成交价格"，因此全零同样算 no_data。"""
    provider, _ = _provider(payload=_ok_payload(bid=[0], ask=[0], last=[0]))

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "no_data"


def test_unconfigured_credential_short_circuits() -> None:
    """凭据无效时不发请求。"""
    provider, fetch = _provider(payload=_ok_payload(), token="   ")

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "not_configured"
    assert not hasattr(fetch, "last_url")


def test_unsupported_instrument_is_rejected_before_any_request() -> None:
    """非标准合约在翻译阶段就被拒。"""
    provider, fetch = _provider(payload=_ok_payload())

    result = provider.fetch_quote(StockInstrument(symbol="lowercase"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "unsupported_instrument"
    assert not hasattr(fetch, "last_url")


def test_invalid_status_field_is_rejected() -> None:
    """`s` 不是 "ok" 时拒绝。"""
    provider, _ = _provider(payload=_ok_payload(s="weird"))

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteUnavailable)
    assert result.reason == "invalid_response"


def test_missing_timestamp_falls_back_to_now() -> None:
    """时间戳缺失或离谱时退回当前时间，而不是产出 1970 年的时点。"""
    provider, _ = _provider(payload=_ok_payload(updated=["bad"]))

    result = provider.fetch_quote(StockInstrument(symbol="INTC"), now_ms=NOW_MS)

    assert isinstance(result, QuoteAvailable)
    assert result.quote.bid is not None
    assert result.quote.bid.as_of == "2025-10-09T08:53:20.000Z"
