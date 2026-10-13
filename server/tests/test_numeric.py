"""数值渲染测试。

``decimal_text`` 是**跨语言一致性的收敛点**：业务哈希用它、公开响应用它、
错误消息也用它。任何一处与 decimal.js 不同，两侧就会对同一个数得到不同文本。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from lyra.domain.numeric import decimal_text


def test_trailing_zeros_are_dropped() -> None:
    """去掉尾随零——``str(Decimal)`` 会保留标度，而公开契约不保留。"""
    assert decimal_text(Decimal("10.000000")) == "10"
    assert decimal_text(Decimal("2.500")) == "2.5"


def test_zero_has_no_sign() -> None:
    """零不带符号。"""
    assert decimal_text(Decimal("0")) == "0"


# --------------------------------------------------------------------------
# decimal.js 的指数形式边界
# --------------------------------------------------------------------------

# 下表由**实际运行 decimal.js 10.6.0 得到**，不是推测：
#
#     node -e "const D=require('decimal.js'); ..."
#
# 保留原始输出而不是"按规则推导"，因为这类边界的价值全在细节上——指数是否带 `+`、
# 是否补零、1e-6 到底走不走指数形式，都只能实测。
_DECIMAL_JS_CASES = [
    ("2", "2"),
    ("2.5", "2.5"),
    ("0.000001", "0.000001"),        # 1e-6 仍是定点（阈值是 < 1e-7）
    ("0.0000001", "1e-7"),           # 刚好落在指数区
    ("0.00000012", "1.2e-7"),
    ("-0.0000001", "-1e-7"),
    ("0.000000123456", "1.23456e-7"),
    ("1.5e-7", "1.5e-7"),
    ("1e20", "100000000000000000000"),
    ("1e21", "1e+21"),               # 上边界带 `+`
    ("1.5e21", "1.5e+21"),
    ("-1e21", "-1e+21"),
    ("0", "0"),
    ("10.000000", "10"),             # 去尾随零
]


@pytest.mark.parametrize(("raw", "expected"), _DECIMAL_JS_CASES)
def test_decimal_text_matches_decimal_js(raw: str, expected: str) -> None:
    """**渲染结果与 decimal.js 逐字符一致。**

    业务哈希用它、错误消息也用它；任何一处不同都会让两侧对同一个数得到不同文本。
    """
    assert decimal_text(Decimal(raw)) == expected


def test_negative_zero_renders_as_plain_zero() -> None:
    """``-0`` 渲染为 ``0``，不带符号——否则两侧会对同一个零得到不同文本。"""
    assert decimal_text(Decimal("-0")) == "0"
    assert decimal_text(Decimal("-0.0")) == "0"


def test_the_exponential_boundary_is_exactly_at_one_e_minus_seven() -> None:
    """**边界精确在 1e-7。**

    这不是理论问题：低价期权的单位成本、拆股调整后的单价都可能落在这一量级。
    本模块此前把指数形式差异记作"只影响极端量级"——实测证明那个定性是错的。
    """
    assert decimal_text(Decimal("0.000001")) == "0.000001"    # 1e-6：定点
    assert decimal_text(Decimal("0.0000001")) == "1e-7"       # 1e-7：指数


def test_the_exponential_boundary_is_exactly_at_one_e_twenty_one() -> None:
    """上边界精确在 1e21。"""
    assert decimal_text(Decimal("1e20")) == "100000000000000000000"
    assert decimal_text(Decimal("1e21")) == "1e+21"


# --------------------------------------------------------------------------
# 两种舍入模式（参考实现刻意混用）
# --------------------------------------------------------------------------

# 下表由**实际运行 decimal.js 10.6.0 的 `toFixed` 得到**：
#
#     node -e "const D=require('decimal.js'); new (D.default||D)('1.23445').toFixed(4)"
#
# 关键在中点值：`toFixed` 平局远离零，而 `ROUND_HALF_EVEN` 会取偶数。
_TO_FIXED_CASES = [
    ("1.23456", "1.2346"),
    ("1.23455", "1.2346"),
    ("1.23445", "1.2345"),   # 银行家舍入会给出 1.2344
    ("-1.23445", "-1.2345"),
    ("1.99995", "2.0000"),
    ("0.00005", "0.0001"),   # 银行家舍入会给出 0.0000
    ("-0.00005", "-0.0001"),
    ("0.00001", "0.0000"),
]


@pytest.mark.parametrize(("raw", "expected"), _TO_FIXED_CASES)
def test_fixed_text_matches_to_fixed(raw: str, expected: str) -> None:
    """**金额用四舍五入（平局远离零）**，与 ``toFixed`` 一致。

    差异只出现在**恰好落在中点**的值上，而账本里"第 5 位为 5"的费用分摊并不罕见。
    """
    from lyra.domain.numeric import fixed_text

    assert fixed_text(Decimal(raw), 4) == expected


def test_percentages_use_bankers_rounding() -> None:
    """**百分比用银行家舍入**——与金额的模式刻意不同。

    参考实现的相邻两行显式写了不同模式：

    ```ts
    .toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN).toFixed(2)   // 百分比
    averageLotPnl.toFixed(4)                                  // 金额
    ```

    **两行相邻却用不同舍入，说明这是有意的区分**，不是笔误。
    """
    from lyra.domain.numeric import percent_text

    assert percent_text(Decimal("1.005")) == "1.00"    # 银行家：取偶数
    assert percent_text(Decimal("1.015")) == "1.02"    # 银行家：取偶数
    # 若误用四舍五入，1.005 会变成 1.01 —— 两者在报表对账时会被发现。


def test_fixed_text_never_renders_negative_zero() -> None:
    """**负数舍入到零时不带负号。**

    ``format(Decimal("-0.00001").quantize(...), "f")`` 会给出 ``-0.0000``，
    而 decimal.js 给出 ``0.0000``——两者会被客户端当成不同的文本。
    """
    from lyra.domain.numeric import fixed_text

    from lyra.domain.numeric import percent_text

    assert fixed_text(Decimal("-0.00001"), 4) == "0.0000"
    assert fixed_text(Decimal("-0.0"), 4) == "0.0000"
    assert percent_text(Decimal("-0.001")) == "0.00"


def test_fixed_text_rejects_a_negative_place_count() -> None:
    """位数不能为负。"""
    from lyra.domain.numeric import fixed_text

    with pytest.raises(ValueError, match="小数位数"):
        fixed_text(Decimal("1"), -1)
