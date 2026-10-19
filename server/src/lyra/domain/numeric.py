"""领域层的数值渲染。

Python 的 ``str(Decimal)`` 保留标度（``Decimal("10.000000")`` 渲染为
``10.000000``），而现有实现使用的 decimal.js ``toString()`` 会去掉尾随零
（渲染为 ``10``）。两者在**业务哈希**上必须一致、在**错误消息与告警**上
应当一致，因此统一收敛到这里，避免各处各写一份。

**decimal.js 还会在两端改用指数形式**（默认阈值 ``toExpPos = 21``、
``toExpNeg = -7``，本项目未改动它们）：

| 输入 | decimal.js |
| --- | --- |
| ``0.000001``（1e-6） | ``0.000001``（仍定点） |
| ``0.0000001``（1e-7） | ``1e-7`` |
| ``0.00000012`` | ``1.2e-7`` |
| ``1e21`` | ``1e+21`` |
| ``1.5e21`` | ``1.5e+21`` |

这不是纯粹的极端量级问题：**1e-7 对金融计算并不罕见**（低价期权的单位成本、
拆股调整后的单价都可能落在这一量级）。此前本模块的注释把它写成"只影响未被跨语言
验证的极端量级"，**那个定性是错的**——实测确认了差异，因此照参考实现复刻。
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal


# decimal.js 的指数形式切换阈值（默认值，本项目未改动）。
_TO_EXP_NEG = -7
_TO_EXP_POS = 21


def decimal_text(value: Decimal) -> str:
    """按 decimal.js ``toString()`` 渲染：去尾随零，两端改用指数形式。

    @param value: 待渲染的精确十进制数。
    @returns 与 decimal.js ``toString()`` 逐字符一致的文本。
    """
    if value == 0:
        # 与 decimal.js 一致：零不带符号，避免 -0 与 0 渲染成不同文本。
        return "0"

    normalized = value.normalize()
    adjusted = normalized.adjusted()
    if _TO_EXP_NEG < adjusted < _TO_EXP_POS:
        return format(normalized, "f")

    # 指数形式。**格式与 decimal.js 逐字符一致**：正指数带 ``+``，负指数带 ``-``，
    # 指数不补零，尾数去尾随零（已由 normalize 保证）。
    sign, digits, exponent = normalized.as_tuple()
    mantissa = str(digits[0])
    if len(digits) > 1:
        mantissa += "." + "".join(str(digit) for digit in digits[1:])
    prefix = "-" if sign else ""
    return f"{prefix}{mantissa}e{'+' if adjusted >= 0 else '-'}{abs(adjusted)}"


# decimal.js ``toFixed`` 与 Python 舍入模式的对应关系。
#
# **注意本项目刻意混用两种舍入**（参考实现 `trade-analysis.ts:139-142` 的相邻两行
# 显式写了不同模式）：
#
# - **金额**用 ``toFixed`` → 四舍五入（平局远离零）；
# - **百分比**用 ``toDecimalPlaces(2, ROUND_HALF_EVEN)`` → 银行家舍入。
#
# 二者只在中点值上不同，而"恰好落在第 5 位为 5"的费用分摊并不罕见，因此不能混为一谈。
_AMOUNT_QUANTUM = Decimal("0.0001")
_PERCENT_QUANTUM = Decimal("0.01")


def fixed_text(value: Decimal, places: int) -> str:
    """按 ``toFixed(places)`` 渲染：四舍五入（平局远离零）。

    **结果的符号与原值一致**（decimal.js 的 ``toFixed`` 只在原值为零时省略负号）：
    ``new Decimal('-0.00001').toFixed(4)`` 是 ``"-0.0000"``，不是 ``"0.0000"``——
    这个断言此前写反了。与 :func:`percent_text` 的差异见该函数说明。

    @param value: 待渲染的数。
    @param places: 小数位数。
    @returns 定点文本。
    @raises ValueError: ``places`` 为负。
    """
    if places < 0:
        raise ValueError("小数位数不能为负")
    quantum = Decimal(1).scaleb(-places)
    quantized = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if quantized == 0 and not (value != 0 and value.is_signed()):
        # **只有在原值本身为零时才去掉负号**：decimal.js 的 toFixed 是
        # ``x.isNeg() && !x.isZero() ? '-' + str : str``，舍入到零的非零负值仍带负号。
        quantized = quantized.copy_abs()
    return format(quantized, "f")


def fixed_point_text(value: Decimal) -> str:
    """按 decimal.js ``toFixed()``（无参）渲染：定点、去尾随零、不用指数形式。

    **与 :func:`decimal_text` 的差别只在量级两端**：``|v| < 1e-6`` 或 ``|v| >= 1e21``
    时 ``toString()`` 改用指数形式，而 ``toFixed()`` 始终给出定点文本。持仓估值与
    参考价中间价在参考实现里走的正是 ``toFixed()``，用 ``toString()`` 会让极小量级
    的金额（例如拆股调整后的单价）渲染成 ``1e-7`` 这样的指数文本。

    @param value: 待渲染的精确十进制数。
    @returns 定点文本。
    """
    if value == 0:
        # decimal.js 对零不补负号（与 :func:`fixed_text` 的判据一致）。
        return "0"
    return format(value.normalize(), "f")


def percent_text(value: Decimal) -> str:
    """按银行家舍入渲染两位小数百分比。

    **与 :func:`fixed_text` 的舍入模式刻意不同**（见上方说明）。

    **零值不保留负号**：参考实现是 ``toDecimalPlaces(2, ROUND_HALF_EVEN).toFixed(2)``，
    ``toFixed`` 作用在**已经舍入为零**的 Decimal 上，而 decimal.js 对零不补负号——
    因此 ``-0.0001`` 渲染为 ``"0.00"``，与 ``fixed_text(-0.0001, 4) == "-0.0000"`` 不同。

    @param value: 百分数（已乘 100）。
    @returns 两位小数的定点文本。
    """
    quantized = value.quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_EVEN)
    if quantized == 0:
        quantized = quantized.copy_abs()
    return format(quantized, "f")
