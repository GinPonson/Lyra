"""行情领域的类型与标识。

镜像 TypeScript 版 ``domain/market-data/types.ts``。

**标识与供应商无关**：NASDAQ 的 OCC 符号、Alpaca 的 ticker、MarketData 的路径
各不相同，但 Lyra 内部的 ``instrument_key`` 是唯一的。供应商差异只在适配器里翻译，
领域层不做猜测——**猜一个供应商别名会静默指向另一个合约**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Union

MarketQuoteIssue = Literal[
    "not_configured",
    "unsupported_instrument",
    "currency_unsupported",
    "no_data",
    "rate_limited",
    "credentials_rejected",
    "provider_unavailable",
    "invalid_response",
    "timeout",
]

MARKET_QUOTE_ISSUES: tuple[MarketQuoteIssue, ...] = (
    "not_configured", "unsupported_instrument", "currency_unsupported", "no_data",
    "rate_limited", "credentials_rejected", "provider_unavailable", "invalid_response",
    "timeout",
)


@dataclass(frozen=True)
class StockInstrument:
    """美股正股。"""

    symbol: str
    asset_type: Literal["stock"] = "stock"
    market: Literal["US"] = "US"
    currency: Literal["USD"] = "USD"


@dataclass(frozen=True)
class OptionInstrument:
    """美股标准期权合约。"""

    underlying: str
    expiry: str
    strike: str
    option_type: Literal["C", "P"]
    asset_type: Literal["option"] = "option"
    market: Literal["US"] = "US"
    multiplier: Literal["100"] = "100"
    currency: Literal["USD"] = "USD"


MarketInstrument = Union[StockInstrument, OptionInstrument]


@dataclass(frozen=True)
class MarketPricePoint:
    """一个带时点的十进制价格。

    快照时间**不一定**是最后一笔成交的时间，因此它与价格一起传递，而不是由调用方
    另行推测。
    """

    value: str
    as_of: str


def market_instrument_key(instrument: MarketInstrument) -> str:
    """生成与供应商无关的合约标识。

    @param instrument: 股票或期权合约。
    @returns 稳定的标识字符串。
    """
    if isinstance(instrument, StockInstrument):
        return f"US:stock:{instrument.symbol}"
    return (
        f"US:option:{instrument.underlying}:{instrument.expiry}:"
        f"{instrument.option_type}:{instrument.strike}"
    )


@dataclass(frozen=True)
class MarketTiming:
    """报价的时效性。

    ``delayed`` 带延迟分钟数；``end_of_day`` 表示收盘价。**两者都要向用户说明**——
    把延迟报价当成实时报价会让用户据此做决定。
    """

    kind: Literal["delayed", "end_of_day"]
    delay_minutes: int | None


@dataclass(frozen=True)
class MarketQuote:
    """一条行情报价。"""

    instrument_key: str
    provider_id: str
    # 供人阅读的来源说明；应用计算不解释它。
    feed: str
    currency: Literal["USD"]
    bid: MarketPricePoint | None
    ask: MarketPricePoint | None
    last: MarketPricePoint | None
    close: MarketPricePoint | None
    timing: MarketTiming
    fetched_at: str
