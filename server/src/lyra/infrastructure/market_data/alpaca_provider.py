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
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import re
from time import monotonic
from typing import Any, Callable, Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ...domain.market_data.history import (
    MarketHistoryBar,
    MarketHistoryRange,
    MarketHistoryResult,
)
from ...domain.market_data.stock_summary import (
    MarketPreviousClose,
    MarketStockSummary,
)
from ...domain.market_data.types import (
    MarketInstrument,
    MarketQuote,
    MarketQuoteIssue,
    MarketTiming,
    StockInstrument,
)
from ...domain.numeric import fixed_point_text
from .http import MARKET_DATA_TIMEOUT_MILLISECONDS, MarketDataFetch, request_market_json
from .identifiers import (
    is_record,
    market_timestamp,
    price_point,
    provider_instrument,
    valid_credential,
)

DELAY_MILLISECONDS = 16 * 60_000
SUMMARY_DELAY_MILLISECONDS = 15 * 60_000
LOOKBACK_MILLISECONDS = 7 * 24 * 60 * 60_000
STOCK_QUOTES_URL = "https://data.alpaca.markets/v2/stocks"
STOCK_BARS_URL = "https://data.alpaca.markets/v2/stocks"
STOCK_SNAPSHOT_URL = "https://data.alpaca.markets/v2/stocks"
OPTION_BARS_URL = "https://data.alpaca.markets/v1beta1/options/bars"
HISTORY_PAGE_SIZE = 256
HISTORY_MAX_PAGES = 32
HISTORY_MAX_POINTS = HISTORY_PAGE_SIZE * HISTORY_MAX_PAGES
_NEW_YORK = ZoneInfo("America/New_York")
_HISTORY_TIMEFRAMES: dict[MarketHistoryRange, tuple[str, int]] = {
    "1d": ("15Min", 7),
    "1w": ("1Hour", 7),
    "1m": ("1Day", 30),
    "3m": ("1Day", 90),
}
_BAR_MILLISECONDS = {"15Min": 15 * 60_000, "1Hour": 60 * 60_000}

# 两类资产均避开最近 15 分钟；股票使用 SIP 报价，期权使用 OPRA 成交聚合。
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
                    # 分钟 K 线来自 OPRA，不能把期权成交参考价误标为 SIP 买卖报价。
                    feed=(
                        "Alpaca SIP delayed quotes"
                        if is_stock
                        else "Alpaca OPRA delayed minute bars"
                    ),
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

    def fetch_history(
        self,
        instrument: MarketInstrument,
        *,
        history_range: MarketHistoryRange,
        now_ms: int,
        consume_page: Callable[[], str | None] | None = None,
    ) -> MarketHistoryResult:
        """读取完整证券身份对应的延迟历史 bars。

        查询区间只由允许的 range、当前延迟 cutoff 与期权到期日计算。股票固定请求
        SIP 拆股复权；期权请求完整 OCC 标识并保持原始成交口径。所有返回 bar 都经过
        时间窗、完整区间、OHLC、成交量和分页校验，异常数据整体拒绝。

        @param instrument: 已由当前用户持仓推导的股票或完整期权合约。
        @param history_range: 有限的历史区间选择。
        @param now_ms: 当前 UTC 毫秒时间戳。
        @param consume_page: 每发起一个后续页请求前消费共享预算的回调。
        @returns 已校验 bars，或稳定的不可用原因。
        """
        identity = provider_instrument(instrument)
        if not identity.ok:
            return MarketHistoryResult(
                status="unavailable", issue=identity.reason,
                cutoff=_iso(now_ms - DELAY_MILLISECONDS),
            )
        if not valid_credential(self._api_key) or not valid_credential(self._secret_key):
            return MarketHistoryResult(
                status="unavailable", issue="not_configured",
                cutoff=_iso(now_ms - DELAY_MILLISECONDS),
            )
        if history_range not in _HISTORY_TIMEFRAMES:
            return MarketHistoryResult(
                status="unavailable", issue="invalid_response",
                cutoff=_iso(now_ms - DELAY_MILLISECONDS),
            )

        timeframe, days = _HISTORY_TIMEFRAMES[history_range]
        cutoff_ms = now_ms - DELAY_MILLISECONDS
        data_end_ms = min(cutoff_ms, _option_expiry_end_ms(instrument))
        cutoff_text = _iso(data_end_ms)
        start_ms, request_end_ms = _history_window(timeframe, days, data_end_ms)
        if request_end_ms < start_ms:
            return MarketHistoryResult(
                status="unavailable", issue="no_data", cutoff=cutoff_text
            )

        is_stock = isinstance(instrument, StockInstrument)
        feed = "sip" if is_stock else "alpaca_option_bars"
        headers = {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._secret_key,
        }
        page_token: str | None = None
        seen_tokens: set[str] = set()
        parsed_rows: list[tuple[int, date, MarketHistoryBar]] = []
        previous_oldest_ms: int | None = None
        page_count = 0
        point_count = 0
        deadline = monotonic() + self._timeout_ms / 1000

        while True:
            remaining_ms = int((deadline - monotonic()) * 1000)
            if remaining_ms <= 0:
                return MarketHistoryResult(
                    status="unavailable", issue="timeout", feed=feed,
                    fetched_at=_iso(now_ms), cutoff=cutoff_text,
                )
            if page_token is not None and consume_page is not None:
                denial = consume_page()
                if denial is not None:
                    return MarketHistoryResult(
                        status="unavailable", issue=denial, feed=feed,
                        fetched_at=_iso(now_ms), cutoff=cutoff_text,
                    )
            query = {
                "start": _iso(start_ms),
                # 只请求完整 interval 边界之前的数据；仍在下方逐 bar 校验 cutoff。
                "end": _iso(request_end_ms),
                "sort": "desc",
                "limit": str(HISTORY_PAGE_SIZE),
                "timeframe": timeframe,
            }
            if is_stock:
                query.update({"feed": "sip", "adjustment": "split"})
                url = f"{STOCK_BARS_URL}/{_encode(identity.symbol)}/bars?{_query(query)}"
            else:
                query["symbols"] = identity.symbol
                url = f"{OPTION_BARS_URL}?{_query(query)}"
            if page_token is not None:
                query["page_token"] = page_token
                if is_stock:
                    url = f"{STOCK_BARS_URL}/{_encode(identity.symbol)}/bars?{_query(query)}"
                else:
                    url = f"{OPTION_BARS_URL}?{_query(query)}"

            response = request_market_json(
                url=url, headers=headers, fetch=self._fetch,
                timeout_ms=remaining_ms,
            )
            if not response.ok:
                return MarketHistoryResult(
                    status="unavailable", issue=response.reason, feed=feed,
                    fetched_at=_iso(now_ms), cutoff=cutoff_text,
                )
            if response.status == 404:
                return MarketHistoryResult(
                    status="unavailable", issue="provider_unavailable", feed=feed,
                    fetched_at=_iso(now_ms), cutoff=cutoff_text,
                )

            try:
                payload = response.body
                if not is_record(payload):
                    raise ValueError("Invalid Alpaca history payload")
                rows = (
                    _stock_history_rows(payload, identity.symbol)
                    if is_stock
                    else _option_rows(payload, identity.symbol)
                )
                if len(rows) > HISTORY_PAGE_SIZE:
                    raise ValueError("Alpaca history page exceeds limit")
                token = payload.get("next_page_token")
                if token is not None and (
                    not isinstance(token, str) or token == "" or token in seen_tokens
                ):
                    raise ValueError("Invalid Alpaca history page token")
                if token is not None:
                    seen_tokens.add(token)

                page_parsed: list[tuple[int, date, MarketHistoryBar]] = []
                page_days: list[date] = []
                previous_in_page: int | None = None
                for row_index, row in enumerate(rows):
                    if not is_record(row):
                        raise ValueError("Invalid Alpaca history row")
                    timestamp = market_timestamp(
                        row.get("t"), start_ms, request_end_ms
                    )
                    start_of_bar = _parse_millis(timestamp)
                    if previous_in_page is not None and start_of_bar >= previous_in_page:
                        raise ValueError("Alpaca history is not descending")
                    previous_in_page = start_of_bar
                    if previous_oldest_ms is not None and row_index == 0:
                        if start_of_bar >= previous_oldest_ms:
                            raise ValueError("Alpaca history pages overlap or are unordered")
                    page_days.append(
                        datetime.fromtimestamp(start_of_bar / 1000, tz=UTC)
                        .astimezone(_NEW_YORK)
                        .date()
                    )
                    if is_stock and payload.get("currency") not in (None, "USD"):
                        raise ValueError("Alpaca stock currency mismatch")
                    parsed_bar = _parse_history_bar(
                        row, timestamp, timeframe, cutoff_ms, data_end_ms
                    )
                    if parsed_bar is not None:
                        parsed_rows.append(parsed_bar)
                        page_parsed.append(parsed_bar)

                point_count += len(rows)
                page_count += 1
                if point_count > HISTORY_MAX_POINTS or page_count > HISTORY_MAX_PAGES:
                    raise ValueError("Alpaca history exceeds pagination bounds")
                if rows:
                    oldest_ms = _parse_millis(rows[-1]["t"])
                    previous_oldest_ms = oldest_ms

                # 1d 只展示最新有成交的纽约民用日期；翻到更早日期后已拿齐目标日。
                if history_range == "1d":
                    traded_dates = [row[1] for row in parsed_rows]
                    if traded_dates:
                        latest_day = max(traded_dates)
                        if any(day < latest_day for day in page_days):
                            break

                if token is None:
                    break
                if page_count >= HISTORY_MAX_PAGES or point_count >= HISTORY_MAX_POINTS:
                    raise ValueError("Alpaca history exceeds pagination bounds")
                if not rows:
                    raise ValueError("Alpaca returned an empty continuation page")
                page_token = token
            except Exception:  # noqa: BLE001 - malformed/incomplete bars fail closed
                return MarketHistoryResult(
                    status="unavailable", issue="invalid_response", feed=feed,
                    fetched_at=_iso(now_ms), cutoff=cutoff_text,
                )

        if history_range == "1d" and parsed_rows:
            latest_day = max(day for _, day, _ in parsed_rows)
            parsed_rows = [row for row in parsed_rows if row[1] == latest_day]
        ordered = tuple(
            item[2] for item in sorted(parsed_rows, key=lambda item: item[0])
        )
        if not ordered:
            return MarketHistoryResult(
                status="unavailable", issue="no_data", feed=feed,
                fetched_at=_iso(now_ms), cutoff=cutoff_text,
            )
        return MarketHistoryResult(
            status="available", bars=ordered, feed=feed,
            fetched_at=_iso(now_ms), cutoff=cutoff_text,
        )

    def fetch_stock_summary(
        self,
        instrument: StockInstrument,
        *,
        now_ms: int,
        consume_request: Callable[[], str | None] | None = None,
    ) -> MarketStockSummary:
        """读取最新延迟成交，并用上一实际交易日拆股复权收盘计算涨跌幅。

        第一请求的 ``latestTrade`` 是真实成交而非 quote midpoint 或历史 bar；第二请求
        读取最多两根完整的 SIP 日 bar，并在跨日时核对成交日 raw/split 收盘口径。缺少
        可验证前收盘时仍保留有效成交价，并明确给出 ``change_issue``。两次请求共享总超时。

        @param instrument: 已由真实持仓标的派生并验证的正股身份。
        @param now_ms: 请求开始时的 UTC 毫秒时间戳。
        @param consume_request: 发出第二次上游请求前消费共享预算的回调。
        @returns 成交摘要或稳定的不可用原因码。
        """
        identity = provider_instrument(instrument)
        if not identity.ok:
            return MarketStockSummary(status="unavailable", issue=identity.reason)
        if not valid_credential(self._api_key) or not valid_credential(self._secret_key):
            return MarketStockSummary(status="unavailable", issue="not_configured")

        feed = "delayed_sip"
        headers = {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._secret_key,
        }
        snapshot_started = monotonic()
        deadline = snapshot_started + self._timeout_ms / 1000
        snapshot_url = (
            f"{STOCK_SNAPSHOT_URL}/{_encode(identity.symbol)}/snapshot"
            "?feed=delayed_sip&currency=USD"
        )
        remaining_ms = int((deadline - monotonic()) * 1000)
        if remaining_ms <= 0:
            return MarketStockSummary(
                status="unavailable", issue="timeout", feed=feed,
                fetched_at=_iso(now_ms),
            )
        response = request_market_json(
            url=snapshot_url, headers=headers, fetch=self._fetch,
            timeout_ms=remaining_ms,
        )
        # Snapshot has no `end` parameter, so the latest trade can advance during the request.
        # Validate its delay against completion time instead of rejecting a newly eligible trade.
        snapshot_completed = monotonic()
        validation_now_ms = now_ms + max(
            0, int((snapshot_completed - snapshot_started) * 1000)
        )
        if not response.ok:
            return MarketStockSummary(
                status="unavailable", issue=response.reason, feed=feed,
                fetched_at=_iso(now_ms),
            )
        if response.status == 404:
            return MarketStockSummary(
                status="unavailable", issue="no_data", feed=feed,
                fetched_at=_iso(now_ms),
            )

        try:
            payload = response.body
            if (
                not is_record(payload)
                or payload.get("symbol") != identity.symbol
                or payload.get("currency") not in (None, "USD")
            ):
                raise ValueError("Invalid Alpaca stock snapshot identity")
            trade = payload.get("latestTrade")
            if trade is None:
                return MarketStockSummary(
                    status="unavailable", issue="no_data", feed=feed,
                    fetched_at=_iso(now_ms),
                )
            if not is_record(trade):
                raise ValueError("Invalid Alpaca latest trade")
            trade_timestamp = market_timestamp(
                trade.get("t"),
                0,
                validation_now_ms,
            )
            trade_ms = _parse_millis(trade_timestamp)
            if trade_ms < validation_now_ms - LOOKBACK_MILLISECONDS:
                return MarketStockSummary(
                    status="unavailable", issue="no_data", feed=feed,
                    fetched_at=_iso(now_ms),
                )
            if trade_ms > validation_now_ms - SUMMARY_DELAY_MILLISECONDS:
                raise ValueError("Alpaca latest trade is inside the delayed cutoff")
            size = trade.get("s")
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                raise ValueError("Invalid Alpaca latest trade size")
            price = price_point(trade.get("p"), _utc_iso_timestamp(trade_timestamp))
            if price is None:
                raise ValueError("Invalid Alpaca latest trade price")
        except Exception:  # noqa: BLE001 - 来源协议错误折叠为稳定原因码
            return MarketStockSummary(
                status="unavailable", issue="invalid_response", feed=feed,
                fetched_at=_iso(now_ms),
            )

        trade_day = datetime.fromtimestamp(trade_ms / 1000, tz=UTC).astimezone(
            _NEW_YORK
        ).date()
        trade_day_start = int(
            datetime.combine(trade_day, time.min, tzinfo=_NEW_YORK).timestamp() * 1000
        )
        previous_close: MarketPreviousClose | None = None
        change_issue: str | None = None

        remaining_ms = int((deadline - monotonic()) * 1000)
        if remaining_ms <= 0:
            change_issue = "timeout"
        else:
            denial = consume_request() if consume_request is not None else None
            if denial is not None:
                change_issue = denial
            else:
                previous_close, change_issue = self._fetch_previous_close(
                    symbol=identity.symbol,
                    trade_day=trade_day,
                    trade_day_start_ms=trade_day_start,
                    snapshot_daily_bar=payload.get("dailyBar"),
                    validation_now_ms=validation_now_ms,
                    deadline=deadline,
                    headers=headers,
                )

        change_percent: str | None = None
        if previous_close is not None:
            with localcontext() as context:
                context.prec = 64
                current = Decimal(price.value)
                previous = Decimal(previous_close.value)
                change_percent = fixed_point_text((current - previous) / previous * 100)

        return MarketStockSummary(
            status="available",
            price=price,
            previous_close=previous_close,
            change_percent=change_percent,
            change_issue=change_issue,
            feed=feed,
            fetched_at=_iso(now_ms),
        )

    def _fetch_previous_close(
        self,
        *,
        symbol: str,
        trade_day: date,
        trade_day_start_ms: int,
        snapshot_daily_bar: object,
        validation_now_ms: int,
        deadline: float,
        headers: dict[str, str],
    ) -> tuple[MarketPreviousClose | None, str | None]:
        """读取前收盘，并在成交日早于快照日时核对 raw 与 split bar 口径。

        @param symbol: 已通过持仓身份校验的股票代码。
        @param trade_day: 最新成交对应的纽约民用日期。
        @param trade_day_start_ms: 该纽约日期午夜对应的 UTC 毫秒时间戳。
        @param snapshot_daily_bar: 快照中可为空的原始日 K，用于证明跨日价格基准一致。
        @param validation_now_ms: snapshot 读取完成时的校验时刻，限制日线免费延迟窗口。
        @param deadline: 两次上游请求共享的 monotonic 总超时截止值。
        @param headers: 仅传给已固定 Alpaca 端点的鉴权请求头。
        @returns 前收盘及稳定失败原因；跨日价格口径无法证明时返回 ``change_unavailable``。
        """
        remaining_ms = int((deadline - monotonic()) * 1000)
        if remaining_ms <= 0:
            return None, "timeout"
        start_ms = int(
            datetime.combine(
                trade_day - timedelta(days=30), time.min, tzinfo=_NEW_YORK
            ).timestamp() * 1000
        )
        trade_day_next_start_ms = int(
            datetime.combine(
                trade_day + timedelta(days=1), time.min, tzinfo=_NEW_YORK
            ).timestamp() * 1000
        )
        bar_cutoff_ms = validation_now_ms - DELAY_MILLISECONDS
        end_ms = min(trade_day_next_start_ms - 1, bar_cutoff_ms)
        snapshot_day = datetime.fromtimestamp(
            validation_now_ms / 1000, tz=UTC
        ).astimezone(_NEW_YORK).date()
        query = {
            "start": _iso(start_ms),
            "end": _iso(end_ms),
            "sort": "desc",
            "limit": "2",
            "timeframe": "1Day",
            "feed": "sip",
            "adjustment": "split",
            "currency": "USD",
        }
        url = f"{STOCK_BARS_URL}/{_encode(symbol)}/bars?{_query(query)}"
        response = request_market_json(
            url=url, headers=headers, fetch=self._fetch, timeout_ms=remaining_ms
        )
        if not response.ok:
            return None, response.reason
        if response.status == 404:
            return None, "no_previous_close"
        try:
            payload = response.body
            if not is_record(payload):
                raise ValueError("Invalid Alpaca previous close payload")
            if payload.get("currency") not in (None, "USD"):
                raise ValueError("Invalid Alpaca previous close currency")
            rows = _stock_history_rows(payload, symbol)
            if not rows:
                return None, (
                    "change_unavailable"
                    if trade_day < snapshot_day
                    else "no_previous_close"
                )
            if len(rows) > 2 or any(not is_record(row) for row in rows):
                raise ValueError("Invalid Alpaca previous close rows")
            completed_bars: dict[date, MarketHistoryBar] = {}
            previous_timestamp_ms: int | None = None
            for row in rows:
                timestamp = market_timestamp(row.get("t"), start_ms, end_ms)
                timestamp_ms = _parse_millis(timestamp)
                if previous_timestamp_ms is not None and timestamp_ms >= previous_timestamp_ms:
                    raise ValueError("Alpaca daily bars are not strictly descending")
                previous_timestamp_ms = timestamp_ms
                parsed = _parse_history_bar(
                    row,
                    timestamp,
                    "1Day",
                    bar_cutoff_ms,
                    trade_day_next_start_ms,
                )
                if parsed is None:
                    continue
                _, session_day, bar = parsed
                if session_day in completed_bars:
                    raise ValueError("Alpaca daily bars contain duplicate sessions")
                completed_bars[session_day] = bar

            if trade_day < snapshot_day:
                split_trade_day = completed_bars.get(trade_day)
                raw_trade_day_close = _snapshot_daily_close(
                    snapshot_daily_bar,
                    trade_day=trade_day,
                    earliest_ms=start_ms,
                    latest_ms=validation_now_ms,
                )
                # The last trade is raw while historical bars are split-adjusted. Only compare
                # across dates if the snapshot's raw daily close proves the adjustment factor
                # for its trade day is still 1:1; otherwise keep price and omit the change.
                if (
                    split_trade_day is None
                    or raw_trade_day_close is None
                    or Decimal(split_trade_day.close) != raw_trade_day_close
                ):
                    return None, "change_unavailable"

            baseline = [
                (session_day, bar)
                for session_day, bar in completed_bars.items()
                if session_day < trade_day
            ]
            if not baseline:
                return None, "no_previous_close"
            session_day, bar = max(baseline, key=lambda entry: entry[0])
            return MarketPreviousClose(bar.close, session_day.isoformat()), None
        except Exception:  # noqa: BLE001 - malformed daily bars cannot define a comparison
            return None, "invalid_response"


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


def _utc_iso_timestamp(value: str) -> str:
    """把已校验的供应商时间转为 UTC ISO，同时保留其小数秒精度。

    @param value: ``market_timestamp`` 已验证的 RFC 3339 时间文本。
    @returns 以 ``Z`` 结尾的 UTC 时间文本。
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    utc_second = (
        parsed.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat(timespec="seconds")
        .removesuffix("+00:00")
    )
    fraction = re.search(r"\.(\d+)(?:Z|[+-]\d{2}:\d{2})$", value)
    fractional_seconds = "" if fraction is None else f".{fraction.group(1)}"
    return f"{utc_second}{fractional_seconds}Z"


def _encode(value: str) -> str:
    """URL 编码路径片段。"""
    from urllib.parse import quote

    return quote(value, safe="")


def _query(values: dict[str, str]) -> str:
    """编码已验证的历史查询参数与上游分页 token。"""
    return urlencode(values)


def _option_expiry_end_ms(instrument: MarketInstrument) -> int:
    """计算查询历史不得越过的期权到期边界。

    @param instrument: 已验证的股票或期权身份。
    @returns 期权到期次日纽约午夜的毫秒时间戳；股票返回可表达的最大边界。
    """
    if isinstance(instrument, StockInstrument):
        return 9_007_199_254_740_991
    expiry = date.fromisoformat(instrument.expiry)
    boundary = datetime.combine(expiry + timedelta(days=1), time.min, tzinfo=_NEW_YORK)
    return int(boundary.timestamp() * 1000)


def _history_window(
    timeframe: str,
    days: int,
    data_end_ms: int,
) -> tuple[int, int]:
    """计算有限的历史请求窗口与完整 bar 的最后允许起点。

    @param timeframe: provider 时间粒度。
    @param days: 最大回看日数。
    @param data_end_ms: 16 分钟 cutoff 与期权到期边界中的较早者。
    @returns 闭区间起点与上游 end 毫秒时间戳。
    """
    if timeframe == "1Day":
        end_day = datetime.fromtimestamp(data_end_ms / 1000, tz=UTC).astimezone(
            _NEW_YORK
        ).date()
        start_day = end_day - timedelta(days=days)
        start = datetime.combine(start_day, time.min, tzinfo=_NEW_YORK)
        # 日 K 的区间是纽约民用日；用 IANA 时区处理 DST 的 23/25 小时变更。
        end_boundary = datetime.combine(end_day, time.min, tzinfo=_NEW_YORK)
        request_end = end_boundary - timedelta(milliseconds=1)
        return int(start.timestamp() * 1000), int(request_end.timestamp() * 1000)

    interval_ms = _BAR_MILLISECONDS[timeframe]
    start_ms = data_end_ms - days * 86_400_000
    request_end_ms = (data_end_ms // interval_ms) * interval_ms - 1
    return start_ms, request_end_ms


def _stock_history_rows(payload: dict[str, Any], symbol: str) -> list[Any]:
    """读取单股票历史响应，并验证可选 ticker 回显。

    @param payload: provider 解码后的响应对象。
    @param symbol: 本次请求的已验证股票代码。
    @returns 行情 bar 列表。
    @raises ValueError: bars 非列表或 ticker 回显不一致时抛出。
    """
    echoed = payload.get("symbol")
    if echoed is not None and echoed != symbol:
        raise ValueError("Alpaca stock symbol mismatch")
    bars = payload.get("bars")
    if bars is None:
        return []
    if not isinstance(bars, list):
        raise ValueError("Invalid Alpaca stock bars")
    return bars


def _parse_history_bar(
    row: dict[str, Any],
    timestamp: str,
    timeframe: str,
    cutoff_ms: int,
    data_end_ms: int,
) -> tuple[int, date, MarketHistoryBar] | None:
    """校验单根 bar 的时间、OHLC 与成交量。

    @param row: provider 返回的 bar 对象。
    @param timestamp: 已解析且通过请求时间窗校验的 UTC 时间。
    @param timeframe: bar 区间粒度。
    @param cutoff_ms: 最近 16 分钟截止时间。
    @param data_end_ms: 期权到期限制后的最晚数据时间。
    @returns 时间键、纽约交易日和有成交 bar；未完成区间或零成交返回 ``None``。
    @raises ValueError: bar 结构或价格关系非法时抛出。
    """
    start_ms = _parse_millis(timestamp)
    if _has_submillisecond_precision(timestamp):
        raise ValueError("Alpaca history timestamp exceeds millisecond precision")

    utc_start = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
    if timeframe == "1Day":
        local_start = utc_start.astimezone(_NEW_YORK)
        if (local_start.hour, local_start.minute, local_start.second, local_start.microsecond) != (
            0, 0, 0, 0
        ):
            raise ValueError("Alpaca daily bar is not anchored at New York midnight")
        local_day = local_start.date()
        end_local = datetime.combine(
            local_day + timedelta(days=1), time.min, tzinfo=_NEW_YORK
        )
        interval_end_ms = int(end_local.timestamp() * 1000)
    else:
        interval_ms = _BAR_MILLISECONDS[timeframe]
        if start_ms % interval_ms != 0:
            raise ValueError("Alpaca intraday bar is not interval-aligned")
        local_day = utc_start.astimezone(_NEW_YORK).date()
        interval_end_ms = start_ms + interval_ms
    if interval_end_ms > min(cutoff_ms, data_end_ms):
        # Alpaca 可包含正在形成的区间；延迟口径只接纳已经完整结束的 bar。
        return None

    volume = row.get("v")
    if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
        raise ValueError("Invalid Alpaca history volume")

    opening = _history_price(row.get("o"))
    high = _history_price(row.get("h"))
    low = _history_price(row.get("l"))
    close = _history_price(row.get("c"))
    if low > min(opening, close) or high < max(opening, close) or high < low:
        raise ValueError("Invalid Alpaca OHLC relationship")
    if volume == 0:
        # 不为无成交区间生成价格点，也不把缺数补成零。
        return None

    return (
        start_ms,
        local_day,
        MarketHistoryBar(
            timestamp=_iso(start_ms),
            open=_history_price_text(opening),
            high=_history_price_text(high),
            low=_history_price_text(low),
            close=_history_price_text(close),
            volume=volume,
        ),
    )


def _history_price(value: object) -> Decimal:
    """解析正有限历史价格，保留原始十进制值。

    @param value: provider 的数字价格字段。
    @returns Decimal 表示的价格。
    @raises ValueError: 类型、正负或有限性不满足行情价格约束时抛出。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid Alpaca history price")
    try:
        price = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Invalid Alpaca history price") from error
    if not price.is_finite() or price <= 0:
        raise ValueError("Invalid Alpaca history price")
    return price


def _snapshot_daily_close(
    value: object,
    *,
    trade_day: date,
    earliest_ms: int,
    latest_ms: int,
) -> Decimal | None:
    """读取 snapshot.dailyBar 的 raw close，仅接受与成交相同纽约日期的有效日 K。

    @param value: snapshot 中可选的 ``dailyBar`` 对象。
    @param trade_day: 最新成交对应的纽约日期。
    @param earliest_ms: 允许的最早时间戳。
    @param latest_ms: 允许的最晚时间戳。
    @returns raw close；缺失、日期不符或字段非法时返回 ``None``。
    """
    if not is_record(value):
        return None
    try:
        timestamp = market_timestamp(value.get("t"), earliest_ms, latest_ms)
        local_day = datetime.fromtimestamp(
            _parse_millis(timestamp) / 1000, tz=UTC
        ).astimezone(_NEW_YORK).date()
        if local_day != trade_day:
            return None
        return _history_price(value.get("c"))
    except (TypeError, ValueError, OverflowError):
        return None


def _history_price_text(value: Decimal) -> str:
    """把历史价格编码为精确、无指数且去除无意义尾零的文本。

    @param value: 已验证的有限价格。
    @returns 不经 Decimal 上下文舍入的定点十进制字符串。
    """
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _has_submillisecond_precision(timestamp: str) -> bool:
    """检查时间戳是否包含不能由毫秒时间键精确保留的精度。

    @param timestamp: provider 返回的 ISO 时间文本。
    @returns 毫秒之后存在非零小数位时为 ``True``。
    """
    match = re.search(r"\.(\d+)(?:Z|[+-]\d{2}:\d{2})$", timestamp)
    return match is not None and any(digit != "0" for digit in match.group(1)[3:])
