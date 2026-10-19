"""Alpaca 适配器测试。

三条校验各防一种静默错误：

1. **符号回显**——防止拿另一个合约的价格来定价；
2. **期权 bar 必须分钟对齐且已走完**——否则把"刚开始的那一分钟"当成收盘价；
3. **零成交 bar 视为无数据**——否则给一个没交易的合约凭空赋值。
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

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


def _summary_provider(responses, *, timeout_ms: int = 8_000):
    """按顺序返回股票快照与日 K 响应，并记录请求参数。"""
    calls: list[dict] = []
    pending = list(responses)

    def fetch(url, *, headers, timeout):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        status, payload = pending.pop(0)
        return FakeResponse(status=status, payload=payload)

    return (
        AlpacaMarketDataProvider(
            api_key="key", secret_key="secret", fetch=fetch, timeout_ms=timeout_ms
        ),
        calls,
    )


def _summary_snapshot(
    *, symbol: str = "INTC", price: float = 12.5,
    timestamp: str = "2026-10-09T19:40:00.123456789Z", size: int = 1,
    daily_timestamp: str = "2026-10-09T04:00:00Z",
    daily_close: float = 12.5,
    include_daily_bar: bool = True,
):
    snapshot = {
        "symbol": symbol,
        "latestTrade": {"p": price, "t": timestamp, "s": size},
    }
    if include_daily_bar:
        snapshot["dailyBar"] = {
            "t": daily_timestamp,
            "o": daily_close,
            "h": daily_close + 1,
            "l": daily_close - 1,
            "c": daily_close,
            "v": 100,
        }
    return snapshot


def _summary_daily_bar(
    *, symbol: str = "INTC", timestamp: str = "2026-10-08T04:00:00Z",
    close: float = 12.0, currency: str = "USD",
    trade_day_timestamp: str = "2026-10-09T04:00:00Z",
    trade_day_close: float = 12.5,
    include_trade_day: bool = True,
    include_previous: bool = True,
):
    rows = []
    if include_trade_day:
        rows.append({
            "t": trade_day_timestamp,
            "o": trade_day_close,
            "h": trade_day_close + 1,
            "l": trade_day_close - 1,
            "c": trade_day_close,
            "v": 100,
        })
    if include_previous:
        rows.append({
            "t": timestamp,
            "o": close,
            "h": close + 1,
            "l": close - 1,
            "c": close,
            "v": 100,
        })
    rows.sort(key=lambda row: row["t"], reverse=True)
    return {
        "symbol": symbol,
        "currency": currency,
        "bars": rows,
    }


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
    assert quote.feed == "Alpaca SIP delayed quotes"


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


def test_stock_summary_aligns_weekend_trade_with_prior_trading_session_close() -> None:
    """周末的快照仍以最后成交纽约日期之前的最近日 bar 作为前收盘。"""
    from datetime import datetime, UTC

    from lyra.domain.market_data.stock_summary import MarketStockSummary

    now_ms = int(datetime(2026, 10, 11, 15, tzinfo=UTC).timestamp() * 1000)
    provider, calls = _summary_provider([
        (200, _summary_snapshot()),
        (200, _summary_daily_bar()),
    ])

    result = provider.fetch_stock_summary(StockInstrument(symbol="INTC"), now_ms=now_ms)

    assert isinstance(result, MarketStockSummary)
    assert result.status == "available"
    assert result.price is not None
    assert result.price.value == "12.5"
    assert result.price.as_of == "2026-10-09T19:40:00.123456789Z"
    assert result.previous_close is not None
    assert result.previous_close.session_date == "2026-10-08"
    assert result.previous_close.value == "12"
    assert result.change_percent is not None
    assert result.change_percent.startswith("4.166666666666666666666666666")
    assert len(calls) == 2

    snapshot_url = urlsplit(calls[0]["url"])
    assert snapshot_url.path == "/v2/stocks/INTC/snapshot"
    assert parse_qs(snapshot_url.query) == {"feed": ["delayed_sip"], "currency": ["USD"]}
    daily_url = urlsplit(calls[1]["url"])
    daily_query = parse_qs(daily_url.query)
    assert daily_url.path == "/v2/stocks/INTC/bars"
    assert daily_query["feed"] == ["sip"]
    assert daily_query["adjustment"] == ["split"]
    assert daily_query["currency"] == ["USD"]
    assert daily_query["timeframe"] == ["1Day"]
    assert daily_query["sort"] == ["desc"] and daily_query["limit"] == ["2"]
    assert daily_query["start"] == ["2026-09-09T04:00:00.000Z"]
    assert daily_query["end"] == ["2026-10-10T03:59:59.999Z"]


def test_stock_summary_cross_day_split_adjustment_mismatch_keeps_only_trade_price() -> None:
    """后来拆股改变了历史 bar 口径时，不把 raw 旧成交与 split 前收相减。"""
    from datetime import datetime, UTC

    now_ms = int(datetime(2026, 10, 11, 15, tzinfo=UTC).timestamp() * 1000)
    provider, calls = _summary_provider([
        (200, _summary_snapshot(price=100.0, daily_close=100.0)),
        (200, _summary_daily_bar(close=49.0, trade_day_close=50.0)),
    ])

    result = provider.fetch_stock_summary(StockInstrument(symbol="INTC"), now_ms=now_ms)

    assert result.status == "available"
    assert result.price is not None and result.price.value == "100"
    assert result.previous_close is None
    assert result.change_percent is None
    assert result.change_issue == "change_unavailable"
    query = parse_qs(urlsplit(calls[1]["url"]).query)
    assert query["limit"] == ["2"]
    assert query["end"] == ["2026-10-10T03:59:59.999Z"]


@pytest.mark.parametrize(
    "snapshot,history",
    [
        (
            _summary_snapshot(include_daily_bar=False),
            _summary_daily_bar(),
        ),
        (
            _summary_snapshot(),
            _summary_daily_bar(include_trade_day=False),
        ),
    ],
)
def test_stock_summary_cross_day_without_both_session_closes_hides_change(
    snapshot, history
) -> None:
    """跨日核对任一侧缺少成交日收盘时，只保留快照成交价。"""
    from datetime import datetime, UTC

    now_ms = int(datetime(2026, 10, 11, 15, tzinfo=UTC).timestamp() * 1000)
    provider, _ = _summary_provider([(200, snapshot), (200, history)])

    result = provider.fetch_stock_summary(StockInstrument(symbol="INTC"), now_ms=now_ms)

    assert result.status == "available"
    assert result.price is not None
    assert result.previous_close is None
    assert result.change_percent is None
    assert result.change_issue == "change_unavailable"


def test_stock_summary_same_day_raw_trade_uses_split_adjusted_prior_close() -> None:
    """成交日与快照日相同，不要求 raw 日收盘核对即可使用拆股复权前收。"""
    from datetime import datetime, UTC

    now_ms = int(datetime(2026, 10, 13, 14, tzinfo=UTC).timestamp() * 1000)
    provider, calls = _summary_provider([
        (200, _summary_snapshot(
            price=51.0,
            timestamp="2026-10-13T13:00:00Z",
            include_daily_bar=False,
        )),
        (200, _summary_daily_bar(
            timestamp="2026-10-12T04:00:00Z",
            close=50.0,
            trade_day_timestamp="2026-10-13T04:00:00Z",
            trade_day_close=55.0,
        )),
    ])

    result = provider.fetch_stock_summary(StockInstrument(symbol="INTC"), now_ms=now_ms)

    assert result.status == "available"
    assert result.price is not None and result.price.value == "51"
    assert result.previous_close is not None
    assert result.previous_close.value == "50"
    assert result.previous_close.session_date == "2026-10-12"
    assert result.change_percent == "2"
    assert result.change_issue is None
    query = parse_qs(urlsplit(calls[1]["url"]).query)
    assert query["end"] == ["2026-10-13T13:44:00.000Z"]


def test_stock_summary_daily_query_obeys_delayed_cutoff_before_ny_midnight() -> None:
    """当纽约日尚未结束时，日线请求上界仍截止于快照时刻前 16 分钟。"""
    from datetime import datetime, UTC

    now_ms = int(datetime(2026, 10, 10, 3, 50, tzinfo=UTC).timestamp() * 1000)
    provider, calls = _summary_provider([
        (200, _summary_snapshot()),
        (200, _summary_daily_bar()),
    ])

    result = provider.fetch_stock_summary(StockInstrument(symbol="INTC"), now_ms=now_ms)

    assert result.status == "available"
    query = parse_qs(urlsplit(calls[1]["url"]).query)
    assert query["end"] == ["2026-10-10T03:34:00.000Z"]
    assert query["limit"] == ["2"]


def test_stock_summary_premarket_trade_uses_previous_calendar_session_close() -> None:
    """盘前最新成交仍对比成交日之前的常规日 bar，而不是当天盘前价。"""
    from datetime import datetime, UTC

    now_ms = int(datetime(2026, 10, 13, 14, tzinfo=UTC).timestamp() * 1000)
    provider, calls = _summary_provider([
        (200, _summary_snapshot(timestamp="2026-10-13T12:30:00.000000001Z")),
        (200, _summary_daily_bar(timestamp="2026-10-12T04:00:00Z", close=10.0)),
    ])

    result = provider.fetch_stock_summary(StockInstrument(symbol="INTC"), now_ms=now_ms)

    assert result.status == "available"
    assert result.price is not None
    assert result.price.as_of == "2026-10-13T12:30:00.000000001Z"
    assert result.previous_close is not None
    assert result.previous_close.session_date == "2026-10-12"
    daily_query = parse_qs(urlsplit(calls[1]["url"]).query)
    assert daily_query["end"] == ["2026-10-13T13:44:00.000Z"]
    assert daily_query["limit"] == ["2"]


def test_stock_summary_retains_trade_when_previous_close_is_missing() -> None:
    """前收盘缺数时保留真实成交价，不伪造零涨跌幅。"""
    from datetime import datetime, UTC

    now_ms = int(datetime(2026, 10, 11, 15, tzinfo=UTC).timestamp() * 1000)
    provider, _ = _summary_provider([
        (200, _summary_snapshot()),
        (200, _summary_daily_bar(include_previous=False)),
    ])

    result = provider.fetch_stock_summary(StockInstrument(symbol="INTC"), now_ms=now_ms)

    assert result.status == "available"
    assert result.price is not None and result.price.value == "12.5"
    assert result.previous_close is None
    assert result.change_percent is None
    assert result.change_issue == "no_previous_close"


@pytest.mark.parametrize(
    "snapshot,expected_issue",
    [
        (_summary_snapshot(symbol="AMD"), "invalid_response"),
        (_summary_snapshot(price=0), "invalid_response"),
        (_summary_snapshot(size=0), "invalid_response"),
        (_summary_snapshot(timestamp="2026-09-01T19:40:00Z"), "no_data"),
        (_summary_snapshot(timestamp="2026-10-11T14:50:00Z"), "invalid_response"),
        (_summary_snapshot(timestamp="2026-10-11T15:00:01Z"), "invalid_response"),
    ],
)
def test_stock_summary_rejects_invalid_latest_trade_identity_price_or_time(
    snapshot, expected_issue
) -> None:
    """symbol 回显、正价格、成交量与七天时窗任一非法都拒绝整个摘要。"""
    from datetime import datetime, UTC

    now_ms = int(datetime(2026, 10, 11, 15, tzinfo=UTC).timestamp() * 1000)
    provider, calls = _summary_provider([(200, snapshot)])

    result = provider.fetch_stock_summary(StockInstrument(symbol="INTC"), now_ms=now_ms)

    assert result.status == "unavailable"
    assert result.issue == expected_issue
    assert len(calls) == 1


@pytest.mark.parametrize(
    "snapshot",
    [
        {"symbol": "INTC"},
        {"symbol": "INTC", "latestTrade": None},
    ],
)
def test_stock_summary_treats_absent_latest_trade_as_no_data(snapshot) -> None:
    """可选的最新成交字段缺失或为 null 表示无成交，而非来源协议损坏。"""
    from datetime import datetime, UTC

    now_ms = int(datetime(2026, 10, 11, 15, tzinfo=UTC).timestamp() * 1000)
    provider, calls = _summary_provider([(200, snapshot)])

    result = provider.fetch_stock_summary(StockInstrument(symbol="INTC"), now_ms=now_ms)

    assert result.status == "unavailable"
    assert result.issue == "no_data"
    assert result.feed == "delayed_sip"
    assert len(calls) == 1


def test_stock_summary_validates_delay_against_snapshot_completion(monkeypatch) -> None:
    """请求期间跨过 15 分钟边界后，应接纳读取完成时已达到延迟要求的成交。"""
    from datetime import datetime, UTC

    import lyra.infrastructure.market_data.alpaca_provider as alpaca_module

    now_ms = int(datetime(2026, 10, 11, 15, tzinfo=UTC).timestamp() * 1000)
    # 该成交比请求开始时的 cutoff 新 100ms，但比读取完成时的 cutoff 旧 100ms。
    trade_time = datetime.fromtimestamp(
        (now_ms - 899_900) / 1000, tz=UTC
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    provider, calls = _summary_provider([
        (200, _summary_snapshot(timestamp=trade_time)),
        (200, _summary_daily_bar()),
    ])
    monotonic_values = iter((100.0, 100.0, 100.2, 100.2, 100.2))
    monkeypatch.setattr(alpaca_module, "monotonic", lambda: next(monotonic_values))

    result = provider.fetch_stock_summary(StockInstrument(symbol="INTC"), now_ms=now_ms)

    assert result.status == "available"
    assert result.price is not None
    assert result.price.as_of == trade_time
    assert result.feed == "delayed_sip"
    assert len(calls) == 2


def test_stock_summary_consumes_second_request_and_preserves_http_failure_reason() -> None:
    """前收盘 HTTP 请求前消费共享预算，稳定错误不泄漏响应正文。"""
    from datetime import datetime, UTC

    now_ms = int(datetime(2026, 10, 11, 15, tzinfo=UTC).timestamp() * 1000)
    provider, calls = _summary_provider([
        (200, _summary_snapshot()),
        (401, {"error": "secret-token-must-not-escape"}),
    ])
    permits: list[bool] = []

    result = provider.fetch_stock_summary(
        StockInstrument(symbol="INTC"),
        now_ms=now_ms,
        consume_request=lambda: permits.append(True) or None,
    )

    assert result.status == "available"
    assert result.price is not None
    assert result.previous_close is None
    assert result.change_issue == "credentials_rejected"
    assert permits == [True]
    assert len(calls) == 2
    assert "secret-token" not in repr(result)


def test_stock_summary_uses_one_total_timeout_for_both_requests(monkeypatch) -> None:
    """首个请求耗尽配置总时限后保留成交，但不再发出前收盘请求。"""
    from datetime import datetime, UTC

    import lyra.infrastructure.market_data.alpaca_provider as alpaca_module

    now_ms = int(datetime(2026, 10, 11, 15, tzinfo=UTC).timestamp() * 1000)
    provider, calls = _summary_provider([(200, _summary_snapshot())], timeout_ms=8_000)
    monotonic_values = iter((0.0, 0.0, 8.1, 8.1))
    monkeypatch.setattr(alpaca_module, "monotonic", lambda: next(monotonic_values))

    result = provider.fetch_stock_summary(StockInstrument(symbol="INTC"), now_ms=now_ms)

    assert result.status == "available"
    assert result.price is not None
    assert result.change_issue == "timeout"
    assert len(calls) == 1
    assert calls[0]["timeout"] == 8.0


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
    assert quote.feed == "Alpaca OPRA delayed minute bars"


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


# --------------------------------------------------------------------------
# 历史 bars
# --------------------------------------------------------------------------


def _history_row(timestamp: str, *, close: float = 2.5, volume: int = 10) -> dict:
    """构造符合 Alpaca bar 形状的固定 OHLC 行。"""
    return {
        "t": timestamp, "o": close - 0.1, "h": close + 0.2,
        "l": close - 0.2, "c": close, "v": volume,
    }


def test_stock_history_uses_split_adjustment_and_only_latest_new_york_trade_date() -> None:
    """股票历史显式 SIP 拆股复权，1d 只返回最新有成交纽约日的完整 bars。"""
    payload = {
        "bars": [
            _history_row("2025-10-09T08:15:00Z", close=12.4),
            _history_row("2025-10-09T08:00:00Z", close=12.3),
            _history_row("2025-10-08T19:45:00Z", close=12.2),
        ],
        "next_page_token": "older-page-is-not-needed",
    }

    def fetch(url, *, headers, timeout):
        fetch.last_url = url  # type: ignore[attr-defined]
        return FakeResponse(payload=payload)

    provider = AlpacaMarketDataProvider(api_key="key", secret_key="secret", fetch=fetch)
    result = provider.fetch_history(
        StockInstrument(symbol="INTC"), history_range="1d", now_ms=NOW_MS
    )

    assert result.status == "available"
    assert result.feed == "sip"
    assert result.cutoff == "2025-10-09T08:37:20.000Z"
    assert [bar.timestamp for bar in result.bars] == [
        "2025-10-09T08:00:00.000Z",
        "2025-10-09T08:15:00.000Z",
    ]
    query = parse_qs(urlsplit(fetch.last_url).query)  # type: ignore[attr-defined]
    assert urlsplit(fetch.last_url).path == "/v2/stocks/INTC/bars"  # type: ignore[attr-defined]
    assert query["timeframe"] == ["15Min"]
    assert query["feed"] == ["sip"]
    assert query["adjustment"] == ["split"]
    assert query["end"] == ["2025-10-09T08:29:59.999Z"]


def test_history_paginates_until_the_requested_range_is_complete() -> None:
    """有下一页时继续读取，不把第一页当成完整 1w 历史。"""
    calls: list[str] = []

    def fetch(url, *, headers, timeout):
        calls.append(url)
        token = parse_qs(urlsplit(url).query).get("page_token")
        if token is None:
            return FakeResponse(payload={
                "bars": [_history_row("2025-10-09T07:00:00Z")],
                "next_page_token": "page-2",
            })
        return FakeResponse(payload={
            "bars": [_history_row("2025-10-09T06:00:00Z")],
            "next_page_token": None,
        })

    provider = AlpacaMarketDataProvider(api_key="key", secret_key="secret", fetch=fetch)
    result = provider.fetch_history(
        StockInstrument(symbol="INTC"), history_range="1w", now_ms=NOW_MS
    )

    assert result.status == "available"
    assert len(calls) == 2
    assert "page_token=page-2" in calls[1]
    assert [bar.timestamp for bar in result.bars] == [
        "2025-10-09T06:00:00.000Z",
        "2025-10-09T07:00:00.000Z",
    ]


def test_expired_option_history_stops_at_expiry_next_new_york_midnight() -> None:
    """过期期权窗口锚定到到期次日纽约午夜，保留到期日完整日 K。"""
    symbol = "INTC250321C00100000"
    calls: list[str] = []

    def fetch(url, *, headers, timeout):
        calls.append(url)
        return FakeResponse(payload={
            "bars": {
                symbol: [
                    _history_row("2025-03-21T04:00:00Z", close=1.5),
                    _history_row("2025-03-20T04:00:00Z", close=1.2),
                ]
            },
            "next_page_token": None,
        })

    provider = AlpacaMarketDataProvider(api_key="key", secret_key="secret", fetch=fetch)
    result = provider.fetch_history(
        OptionInstrument(
            underlying="INTC", expiry="2025-03-21", strike="100",
            option_type="C",
        ),
        history_range="1m",
        now_ms=NOW_MS,
    )

    assert result.status == "available"
    assert result.cutoff == "2025-03-22T04:00:00.000Z"
    assert [bar.timestamp for bar in result.bars] == [
        "2025-03-20T04:00:00.000Z",
        "2025-03-21T04:00:00.000Z",
    ]
    query = parse_qs(urlsplit(calls[0]).query)
    assert query["symbols"] == [symbol]
    assert query["timeframe"] == ["1Day"]
    assert query["end"] == ["2025-03-22T03:59:59.999Z"]


@pytest.mark.parametrize(
    "rows",
    [
        [_history_row("2025-10-09T08:00:00Z"), _history_row("2025-10-09T08:00:00Z")],
        [_history_row("2025-10-09T08:00:00Z"), _history_row("2025-10-09T08:15:00Z")],
        [{**_history_row("2025-10-09T08:00:00Z"), "h": 1.0}],
        [{**_history_row("2025-10-09T08:00:00Z"), "v": 1.5}],
    ],
)
def test_history_rejects_duplicate_unordered_or_invalid_bars(rows: list[dict]) -> None:
    """重复、不可信时间顺序或字段非法的 bar 不进入缓存结果。"""
    provider, _ = _provider(payload={"bars": rows, "next_page_token": None})

    result = provider.fetch_history(
        StockInstrument(symbol="INTC"), history_range="1w", now_ms=NOW_MS
    )

    assert result.status == "unavailable"
    assert result.issue == "invalid_response"


def test_history_drops_zero_volume_bars_without_fabricating_a_price() -> None:
    """零成交 bar 不补进序列，整段无成交时明确为 no_data。"""
    provider, _ = _provider(payload={
        "bars": [_history_row("2025-10-09T07:00:00Z", volume=0)],
        "next_page_token": None,
    })

    result = provider.fetch_history(
        StockInstrument(symbol="INTC"), history_range="1w", now_ms=NOW_MS
    )

    assert result.status == "unavailable"
    assert result.issue == "no_data"
    assert result.bars == ()


def test_history_pagination_shares_one_total_timeout(monkeypatch) -> None:
    """多页请求共享 provider 总超时，每页收到递减的剩余 timeout。"""
    import lyra.infrastructure.market_data.alpaca_provider as alpaca_module

    elapsed = [0.0]
    timeouts: list[float] = []
    pages = [
        {"bars": [_history_row("2025-10-09T07:00:00Z")], "next_page_token": "p2"},
        {"bars": [_history_row("2025-10-09T06:00:00Z")], "next_page_token": None},
    ]

    def fake_fetch(url, *, headers, timeout):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            elapsed[0] = 0.75
        return FakeResponse(payload=pages.pop(0))

    monkeypatch.setattr(alpaca_module, "monotonic", lambda: elapsed[0])
    provider = AlpacaMarketDataProvider(
        api_key="key", secret_key="secret", fetch=fake_fetch, timeout_ms=1_000
    )

    result = provider.fetch_history(
        StockInstrument(symbol="INTC"), history_range="1w", now_ms=NOW_MS
    )

    assert result.status == "available"
    assert timeouts == [1.0, 0.25]


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
