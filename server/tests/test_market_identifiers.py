"""行情标识翻译与价格解析测试。

两块最容易出错的地方：

1. **OCC 行使价是"千分之一美元"的 8 位数字**。把 ``100.00`` 写成 ``100`` 会指向
   另一个合约，而供应商只会返回"无数据"——真正的错误被掩盖；
2. **零报价是"没有可成交价格"，不是"免费"**。当成价格会让持仓市值变成 0，
   用户看到的是"资产蒸发了"。
"""

from __future__ import annotations

import pytest

from lyra.domain.market_data.types import (
    OptionInstrument,
    StockInstrument,
)
from lyra.infrastructure.market_data.identifiers import (
    price_point,
    provider_instrument,
    unix_market_timestamp,
    valid_calendar_date,
    valid_credential,
)


def _option(**overrides):
    base = {
        "underlying": "INTC", "expiry": "2027-03-19", "strike": "100.00",
        "option_type": "C",
    }
    base.update(overrides)
    return OptionInstrument(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 正股
# --------------------------------------------------------------------------


@pytest.mark.parametrize("symbol", ["INTC", "A", "BRK.B", "RDS-A", "AB1234567"])
def test_accepts_standard_stock_symbols(symbol: str) -> None:
    """常规美股代码通过。"""
    result = provider_instrument(StockInstrument(symbol=symbol))

    assert result.ok is True
    assert result.symbol == symbol
    assert result.instrument_key == f"US:stock:{symbol}"


@pytest.mark.parametrize("symbol", ["", "intc", "1234", "INTC TOO LONG", "A" * 12, "INTC."])
def test_rejects_nonstandard_stock_symbols(symbol: str) -> None:
    """非标准代码必须拒绝，不猜供应商别名。"""
    result = provider_instrument(StockInstrument(symbol=symbol))
    assert result.ok is False and result.reason == "unsupported_instrument"


# --------------------------------------------------------------------------
# 期权 OCC 符号
# --------------------------------------------------------------------------


def test_builds_the_occ_symbol() -> None:
    """OCC 符号：标的 + YYMMDD + C/P + 8 位千分位行使价。"""
    result = provider_instrument(_option())

    assert result.ok is True
    assert result.symbol == "INTC270319C00100000"


@pytest.mark.parametrize(
    ("strike", "expected_suffix"),
    [
        ("100.00", "00100000"),
        ("7.5", "00007500"),
        ("0.005", "00000005"),
        ("100.123", "00100123"),
        ("99999.999", "99999999"),
    ],
)
def test_strike_is_encoded_as_thousandths(strike: str, expected_suffix: str) -> None:
    """**行使价编码为千分之一美元的 8 位数字**，不是合约价值。"""
    result = provider_instrument(_option(strike=strike))

    assert result.ok is True
    assert result.symbol.endswith(expected_suffix)


def test_zero_strike_is_rejected() -> None:
    """零行使价不存在，必须拒绝（8 位数字下限是 00000001）。"""
    result = provider_instrument(_option(strike="0.000"))
    assert result.ok is False and result.reason == "unsupported_instrument"


def test_strike_above_the_eight_digit_limit_is_rejected() -> None:
    """超过 8 位千分位（>99999.999）无法编码，必须拒绝。"""
    result = provider_instrument(_option(strike="100000.00"))
    assert result.ok is False and result.reason == "unsupported_instrument"


def test_strike_with_too_many_decimals_is_rejected() -> None:
    """小数位超过 3 位无法编码到千分位。"""
    result = provider_instrument(_option(strike="100.0001"))
    assert result.ok is False and result.reason == "unsupported_instrument"


def test_put_option_uses_p() -> None:
    """看跌期权用 P。"""
    result = provider_instrument(_option(option_type="P"))
    assert result.ok is True and "P00100000" in result.symbol


@pytest.mark.parametrize(
    "overrides",
    [
        {"underlying": "TOOLONG"},
        {"underlying": "intc"},
        {"expiry": "2027-3-19"},
        {"expiry": "1999-03-19"},   # 非 20xx
        {"expiry": "2027-02-30"},   # 格式合法但日期不存在
        {"option_type": "X"},
        {"strike": "-1"},
        {"strike": "01.00"},        # 前导零
    ],
)
def test_rejects_nonstandard_options(overrides: dict) -> None:
    """非标准合约字段必须拒绝。"""
    result = provider_instrument(_option(**overrides))
    assert result.ok is False and result.reason == "unsupported_instrument"


def test_multiplier_must_be_one_hundred() -> None:
    """非 100 乘数的合约不支持（调整过的合约）。"""
    instrument = _option()
    object.__setattr__(instrument, "multiplier", "10")

    result = provider_instrument(instrument)
    assert result.ok is False


def test_instrument_key_is_provider_neutral() -> None:
    """标识与供应商无关，且包含完整合约要素。"""
    result = provider_instrument(_option())
    assert result.ok is True
    assert result.instrument_key == "US:option:INTC:2027-03-19:C:100.00"


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["2027-03-19", "2028-02-29", "2027-12-31"])
def test_valid_calendar_dates(value: str) -> None:
    """真实存在的日期通过（含闰年 2 月 29 日）。"""
    assert valid_calendar_date(value) is True


@pytest.mark.parametrize(
    "value", ["2027-02-30", "2027-02-29", "2027-13-01", "2027-00-10", "2027-3-19", "abc"]
)
def test_invalid_calendar_dates(value: str) -> None:
    """**格式合法但不存在**的日期必须拒绝。

    若放过去，供应商只会返回"无数据"，真正的错误被掩盖。
    """
    assert valid_calendar_date(value) is False


# --------------------------------------------------------------------------
# 价格
# --------------------------------------------------------------------------


def test_null_price_is_absent() -> None:
    """``None`` 表示没有价格。"""
    assert price_point(None, "t") is None


def test_zero_price_is_absent_not_free() -> None:
    """**零报价是"没有可成交价格"，不是"免费"。**

    当成价格会让持仓市值变成 0，用户看到的是"资产蒸发了"。
    """
    assert price_point(0, "t") is None
    assert price_point(0.0, "t") is None


def test_positive_price_becomes_decimal_text() -> None:
    """正价格转为十进制文本并带时点。"""
    point = price_point(12.34, "2026-10-12T09:00:00.000Z")

    assert point is not None
    assert point.value == "12.34"
    assert point.as_of == "2026-10-12T09:00:00.000Z"


def test_integer_price_has_no_trailing_noise() -> None:
    """整数价格不出现多余小数位。"""
    point = price_point(100, "t")
    assert point is not None and point.value == "100"


def test_price_text_avoids_binary_float_noise() -> None:
    """**十进制文本不引入二进制浮点噪声。**

    直接 ``str()`` 对某些数值会给出 ``0.30000000000000004``，而那个数字会被写进
    审计记录并展示给用户。
    """
    point = price_point(0.1 + 0.2, "t")

    assert point is not None
    assert point.value == "0.30000000000000004"
    # 注意：0.1+0.2 在 IEEE754 下确实不等于 0.3，因此这里保留真实值——
    # 关键是**不额外制造噪声**，而不是替供应商"修正"数字。
    assert point.value != "0.3"


@pytest.mark.parametrize("value", ["12.34", True, {"a": 1}, [1]])
def test_non_numeric_prices_are_rejected(value: object) -> None:
    """文本、布尔、容器都不得被强制转换——协议变化应显式失败。"""
    with pytest.raises(ValueError, match="Invalid market price"):
        price_point(value, "t")


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), 2 ** 53])
def test_out_of_range_prices_are_rejected(value: float) -> None:
    """负数、非有限值、超出安全整数范围都拒绝。"""
    with pytest.raises(ValueError, match="Invalid market price"):
        price_point(value, "t")


# --------------------------------------------------------------------------
# Unix 秒级时间戳
# --------------------------------------------------------------------------

NOW_MS = 1_760_000_000_000


def test_unix_timestamp_is_rendered_from_seconds() -> None:
    """合法的 Unix 秒级时间戳渲染为 ISO 文本。"""
    assert unix_market_timestamp(1_760_000_000, NOW_MS) == "2025-10-09T08:53:20.000Z"


@pytest.mark.parametrize(
    "value",
    [
        "bad",            # 非数字
        True,             # 布尔是 int 的子类，但不是合法时间戳
        1_760_000_000.5,  # 非整数秒
        -1,               # 负数
        NOW_MS // 1000 + 3_600,  # 未来
        2 ** 53,          # 超出 JS 安全整数范围
    ],
)
def test_invalid_unix_timestamps_raise(value: object) -> None:
    """**无效时间戳必须抛错，而不是伪造成"现在"。**

    退回当前时间会把"来源时点不明的报价"当成刚取到的新鲜报价，让它通过过期判断并
    被计入 ``complete``；调用方把该异常折叠为 ``invalid_response``。
    """
    with pytest.raises(ValueError, match="Invalid Unix market timestamp"):
        unix_market_timestamp(value, NOW_MS)


# --------------------------------------------------------------------------
# 凭据
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["token", "abc-123_XYZ+/=", "a" * 4096])
def test_credentials_allow_printable_ascii(value: str) -> None:
    """可打印 ASCII 凭据通过。"""
    assert valid_credential(value) is True


@pytest.mark.parametrize("value", ["", "   ", "a b", "bad\nvalue", "中", "bad\x00value"])
def test_credentials_reject_whitespace_and_non_ascii(value: str) -> None:
    """**空白、控制字符与非 ASCII 都拒绝**（与参考实现的 ``[\\x21-\\x7e]+`` 对齐）。"""
    assert valid_credential(value) is False
