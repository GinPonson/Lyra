"""持仓参考估值与币种汇总测试。

三条不可放宽的性质：

1. **方向决定符号**——把空头当多头会让组合总额凭空翻正；
2. **期权乘数是 100**——忘掉它会让期权估值差 100 倍；
3. **不完整时组合计为 `None`**——部分求和会把组合总额**少报一个未知量**，
   而用户看到的是一个看起来正常的数字。
"""

from __future__ import annotations

from decimal import Decimal

from lyra.domain.market_data.valuation import (
    aggregate_currency_group,
    multiplier_for,
    value_position,
)


def _row(market_value, unrealized, *, available=True, as_of="2025-10-09T08:53:20.000Z"):
    """构造一行用于汇总的持仓。"""
    from lyra.domain.market_data.valuation import PositionValuation

    return {
        "valuation": PositionValuation(
            market_value=market_value, unrealized_pnl=unrealized, multiplier="1"
        ),
        "available": available,
        "as_of": as_of,
    }


# --------------------------------------------------------------------------
# 单行估值
# --------------------------------------------------------------------------


def test_long_stock_valuation() -> None:
    """多头正股：市值 = 数量 × 参考价，盈亏 = 市值 − 成本。"""
    valuation = value_position(
        quantity="10", cost="400", reference_price="60",
        asset_type="stock", direction="long",
    )

    assert valuation.market_value == "600"
    assert valuation.unrealized_pnl == "200"
    assert valuation.multiplier == "1"


def test_tiny_market_value_is_rendered_in_fixed_point() -> None:
    """**估值金额用 ``toFixed()`` 口径，极小量级不得变成指数形式。**

    参考价 ``1e-7`` × 数量 1 的市值是 ``0.0000001``；若按 ``toString()`` 渲染会写成
    ``1e-7``，与参考实现和客户端契约都不同。
    """
    valuation = value_position(
        quantity="1", cost="0", reference_price="0.0000001",
        asset_type="stock", direction="long",
    )

    assert valuation.market_value == "0.0000001"
    assert valuation.unrealized_pnl == "0.0000001"


def test_short_position_is_negative() -> None:
    """**空头持仓的市值是负的。**

    把空头当多头会让组合总额凭空翻正——用户会以为自己在赚钱。
    """
    long_side = value_position(
        quantity="10", cost="400", reference_price="60",
        asset_type="stock", direction="long",
    )
    short_side = value_position(
        quantity="10", cost="400", reference_price="60",
        asset_type="stock", direction="short",
    )

    assert Decimal(short_side.market_value) == -Decimal(long_side.market_value)


def test_option_multiplier_is_one_hundred() -> None:
    """**期权乘数是 100**——忘掉它会让估值差 100 倍。"""
    valuation = value_position(
        quantity="2", cost="500", reference_price="3",
        asset_type="option", direction="long",
    )

    assert valuation.multiplier == "100"
    assert valuation.market_value == "600"  # 2 × 3 × 100


def test_multiplier_helper() -> None:
    """乘数按资产类型取值。"""
    assert multiplier_for("option") == "100"
    assert multiplier_for("stock") == "1"


def test_missing_reference_price_yields_no_amounts() -> None:
    """没有参考价时两个金额字段都是 ``None``——**不猜**。"""
    valuation = value_position(
        quantity="10", cost="400", reference_price=None,
        asset_type="stock", direction="long",
    )

    assert valuation.market_value is None
    assert valuation.unrealized_pnl is None
    # 乘数仍然给出：它只取决于资产类型。
    assert valuation.multiplier == "1"


def test_basis_states_the_caliber() -> None:
    """口径必须随金额一起给出。"""
    valuation = value_position(
        quantity="1", cost="1", reference_price="1",
        asset_type="stock", direction="long",
    )
    assert valuation.basis == "reference_price_excluding_fees"


def test_decimal_text_has_no_trailing_noise() -> None:
    """金额渲染为十进制文本，不带浮点噪声。"""
    valuation = value_position(
        quantity="3", cost="0", reference_price="0.1",
        asset_type="stock", direction="long",
    )

    assert valuation.market_value == "0.3"


# --------------------------------------------------------------------------
# 币种汇总
# --------------------------------------------------------------------------


def test_complete_group_sums_the_positions() -> None:
    """全部有新鲜估值时给出合计。"""
    total = aggregate_currency_group([
        _row("600", "200"), _row("300", "100"),
    ])

    assert total.complete is True
    assert total.positions == 2
    assert total.priced_positions == 2
    assert total.market_value == "900"
    assert total.unrealized_pnl == "300"


def test_incomplete_group_reports_no_total() -> None:
    """**不完整时合计为 `None`。**

    部分求和会把组合总额少报一个未知量，而用户看到的是一个看起来正常的数字。
    """
    total = aggregate_currency_group([
        _row("600", "200"), _row(None, None, available=False),
    ])

    assert total.complete is False
    assert total.priced_positions == 1
    # 有一行能算，但**不给出合计**。
    assert total.market_value is None
    assert total.unrealized_pnl is None


def test_stale_rows_are_priced_but_not_complete() -> None:
    """**过期报价的行有估值但不算完整。**

    "能算出行级估值"与"能给组合合计"是两件事，混同会让用户拿到一个偏小的总额。
    """
    total = aggregate_currency_group([
        _row("600", "200", available=True),
        _row("300", "100", available=False),
    ])

    assert total.priced_positions == 2   # 两行都有估值
    assert total.complete is False       # 但有一行不新鲜
    assert total.market_value is None


def test_empty_group_is_complete_with_zero() -> None:
    """空分组：完整且合计为零（没有未定价的持仓）。"""
    total = aggregate_currency_group([])

    assert total.positions == 0
    assert total.complete is True
    assert total.market_value == "0"
    assert total.as_of_from is None


def test_as_of_range_tracks_the_priced_rows() -> None:
    """时点范围覆盖所有已定价行。"""
    total = aggregate_currency_group([
        _row("600", "200", as_of="2025-10-09T08:53:20.000Z"),
        _row("300", "100", as_of="2025-10-09T07:00:00.000Z"),
    ])

    assert total.as_of_from == "2025-10-09T07:00:00.000Z"
    assert total.as_of_to == "2025-10-09T08:53:20.000Z"


def test_unpriced_rows_do_not_contribute_a_timestamp() -> None:
    """没有估值的行不参与时点范围。"""
    total = aggregate_currency_group([
        _row(None, None, available=False, as_of=None),
    ])

    assert total.as_of_from is None
    assert total.as_of_to is None


def test_sum_is_exact_for_decimals() -> None:
    """合计使用精确十进制，不引入浮点误差。"""
    total = aggregate_currency_group([
        _row("0.1", "0"), _row("0.2", "0"),
    ])

    assert total.market_value == "0.3"
