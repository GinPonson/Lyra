"""供应商合约标识的翻译与价格解析。

镜像 TypeScript 版 ``infrastructure/market-data/identifiers.ts``。

两处容易出错的地方：

1. **OCC 符号的行使价是"千分之一美元"的 8 位数字**，不是合约价值。把 ``100.00``
   写成 ``100`` 会指向另一个合约（甚至不存在），而供应商只会返回"无数据"。
2. **零报价是"没有可成交价格"，不是"免费"。** 把 0 当成价格会让持仓市值变成 0，
   用户看到的是"资产蒸发了"。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal, InvalidOperation
from typing import Literal

from ...domain.market_data.types import (
    MarketInstrument,
    MarketPricePoint,
    MarketQuoteIssue,
    StockInstrument,
    market_instrument_key,
)

# OCC 行使价：最多 5 位整数部分，整体最多 8 位千分位（即 99999.999）。
MAX_OCC_STRIKE_THOUSANDTHS = 99_999_999

_STOCK_SYMBOL = re.compile(r"^[A-Z][A-Z0-9]{0,9}(?:[.-][A-Z0-9]{1,2})?$")
_UNDERLYING = re.compile(r"^[A-Z]{1,6}$")
_STRIKE = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d{1,3})?$")
_OPTION_EXPIRY = re.compile(r"^20\d{2}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class ProviderInstrumentOk:
    """可翻译的合约。"""

    ok: Literal[True]
    symbol: str
    instrument_key: str


@dataclass(frozen=True)
class ProviderInstrumentRejected:
    """不可翻译的合约及原因。"""

    ok: Literal[False]
    reason: MarketQuoteIssue


ProviderInstrument = ProviderInstrumentOk | ProviderInstrumentRejected


def provider_instrument(instrument: MarketInstrument) -> ProviderInstrument:
    """把内部合约翻译为供应商标识。

    只支持美股 USD 标准合约；非标准标的（调整过的期权代码等）**直接拒绝**，
    不猜测供应商别名。

    @param instrument: 内部合约。
    @returns 可翻译结果或拒绝原因。
    """
    if instrument.currency != "USD":
        return ProviderInstrumentRejected(ok=False, reason="currency_unsupported")
    if instrument.market != "US":
        return ProviderInstrumentRejected(ok=False, reason="unsupported_instrument")

    if isinstance(instrument, StockInstrument):
        if not _STOCK_SYMBOL.fullmatch(instrument.symbol):
            return ProviderInstrumentRejected(ok=False, reason="unsupported_instrument")
        return ProviderInstrumentOk(
            ok=True,
            symbol=instrument.symbol,
            instrument_key=market_instrument_key(instrument),
        )

    if (
        not _UNDERLYING.fullmatch(instrument.underlying)
        or not _OPTION_EXPIRY.fullmatch(instrument.expiry)
        or not valid_calendar_date(instrument.expiry)
        or not _STRIKE.fullmatch(instrument.strike)
        or instrument.option_type not in ("C", "P")
        or instrument.multiplier != "100"
    ):
        return ProviderInstrumentRejected(ok=False, reason="unsupported_instrument")

    whole, _, fraction = instrument.strike.partition(".")
    # 8 位 OCC 数字表示千分之一美元，不是合约价值。
    if len(whole) > 5:
        return ProviderInstrumentRejected(ok=False, reason="unsupported_instrument")
    thousandths = int(whole) * 1000 + int(fraction.ljust(3, "0") or "0")
    if thousandths <= 0 or thousandths > MAX_OCC_STRIKE_THOUSANDTHS:
        return ProviderInstrumentRejected(ok=False, reason="unsupported_instrument")

    expiry = instrument.expiry[2:].replace("-", "")
    return ProviderInstrumentOk(
        ok=True,
        symbol=(
            f"{instrument.underlying}{expiry}{instrument.option_type}"
            f"{str(thousandths).zfill(8)}"
        ),
        instrument_key=market_instrument_key(instrument),
    )


def valid_calendar_date(value: str) -> bool:
    """判断 ``YYYY-MM-DD`` 是否是真实存在的日期。

    **``2027-02-30`` 必须被拒**：它格式合法但不存在，供应商会把它当成无效请求，
    而错误信息只会说"无数据"——真正的错误被掩盖了。
    """
    if not _OPTION_EXPIRY.fullmatch(value):
        return False
    year, month, day = (int(part) for part in value.split("-"))
    if not 1 <= month <= 12:
        return False
    return 1 <= day <= calendar.monthrange(year, month)[1]


def is_record(value: object) -> bool:
    """判断是否为 JSON 对象（排除数组与 ``None``）。"""
    return isinstance(value, dict)


def price_point(value: object, as_of: str) -> MarketPricePoint | None:
    """把供应商返回的价格解析为十进制文本。

    @param value: 供应商返回的原始值。
    @param as_of: 该价格的时点。
    @returns 价格点；``None`` 表示"没有可成交价格"。
    @raises ValueError: 值不是有限非负数值。
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # 不强制转换文本或布尔：那会把供应商的协议变化变成静默的错误价格。
        raise ValueError("Invalid market price")
    if not _is_finite(value) or value < 0 or value > 9_007_199_254_740_991:
        raise ValueError("Invalid market price")
    # **零报价是"没有可成交价格"，不是"免费"。** 当成价格会让市值变成 0。
    if value == 0:
        return None
    return MarketPricePoint(value=_decimal_text(value), as_of=as_of)


def _is_finite(value: float) -> bool:
    """判断数值有限（排除 NaN 与 Infinity）。"""
    import math

    return math.isfinite(value)


def _decimal_text(value: float) -> str:
    """把 JSON 数值渲染为十进制文本，**不引入浮点噪声**。

    ``0.1`` 在二进制浮点里不是精确值；直接 ``str()`` 有时能得到 ``0.1``，但对
    其它数值会给出 ``0.30000000000000004`` 这类结果，而那个数字会被写进审计记录。
    """
    try:
        return format(Decimal(repr(value)).normalize(), "f")
    except InvalidOperation:  # pragma: no cover - 非有限值已在上游排除
        raise ValueError("Invalid market price") from None


def unix_market_timestamp(value: object, now: int) -> str:
    """把供应商的 Unix 秒级时间戳渲染为 ISO 文本。

    **无效或明显离谱的时间戳退回当前时间**，而不是产出一个 1970 年或 55000 年的
    时点——那会让"报价是否过期"的判断彻底失效。

    @param value: 供应商返回值。
    @param now: 当前毫秒时间戳。
    @returns ISO UTC 文本。
    """

    if isinstance(value, bool) or not isinstance(value, int):
        return _iso_ms(now)
    # **任何未来时间戳都不可信**：供应商的时钟快一点就会把"报价时间"推到未来，
    # 让"是否过期"的判断反向失效。
    if value < 0 or value * 1000 > now:
        return _iso_ms(now)
    return _iso_ms(value * 1000)


def valid_credential(value: str) -> bool:
    """判断凭据是否可用（非空且无控制字符）。

    控制字符会破坏 HTTP 头，而请求头的解析失败通常表现为"认证失败"——掩盖真正原因。
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if stripped == "" or len(stripped) > 4096:
        return False
    return not any(ord(character) < 0x20 or ord(character) == 0x7F for character in stripped)


def _iso_ms(milliseconds: int) -> str:
    """把毫秒时间戳渲染为与 JavaScript ``toISOString()`` 同形的文本。"""
    from datetime import datetime

    return (
        datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"
)


def market_timestamp(value: object, earliest_ms: int, latest_ms: int) -> str:
    """校验并**原样保留**供应商的时间戳文本。

    保留原文是刻意的：Alpaca 返回纳秒精度，重新格式化会丢掉精度，而那个精度会被
    写进报价的 ``asOf`` 并用于过期判断。

    @param value: 供应商返回的时间戳。
    @param earliest_ms: 允许的最早时间（毫秒）。
    @param latest_ms: 允许的最晚时间（毫秒）。
    @returns 原样时间戳。
    @raises ValueError: 格式非法、日期不存在，或落在窗口之外。
    """
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError("Invalid market timestamp")
    if not valid_calendar_date(value[:10]):
        raise ValueError("Invalid market timestamp")
    milliseconds = _parse_millis(value)
    if milliseconds < earliest_ms or milliseconds > latest_ms:
        raise ValueError("Market timestamp outside requested window")
    return value


_TIMESTAMP_PARTS = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})"
)


def _parse_millis(value: str) -> int:
    """解析带纳秒精度的时间戳为毫秒。

    Python 的 ``datetime`` 只支持微秒，因此把小数部分补/截到 6 位后再解析。
    用正则精确拆分而不是字符串切分——后者的偏移量处理极易出错。
    """
    from datetime import datetime

    match = _TIMESTAMP_PARTS.fullmatch(value)
    if match is None:
        raise ValueError("Invalid market timestamp")
    head, fraction, offset = match.group(1), match.group(2) or "", match.group(3)
    microseconds = (fraction + "000000")[:6]
    normalized = f"{head}.{microseconds}{offset.replace('Z', '+00:00')}"
    return int(datetime.fromisoformat(normalized).timestamp() * 1000)
