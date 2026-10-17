"""`build_trade` 勾稽校验测试。

两处校验都用 0.011 的容差吸收显示舍入；**超出容差即报错而不是修正**——修正会让账本
记下一个从未发生过的数字，而用户看到的是"金额对上了"。

**买入的 `amount` 取负**：现金流出为负是账本的符号约定；不取负会让买入看起来像收入，
现金流与持仓方向同时反了。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from lyra.domain.gmail.econfirm import (
    BUY_ACTIONS,
    RECONCILIATION_TOLERANCE,
    build_trade,
)
from lyra.domain.ledger.errors import IngestError


def _trade(**overrides):
    """构造一笔成交。"""
    base = {
        "trade_date": "2026-01-05", "action": "Buy", "symbol": "INTC",
        "description": "INTEL CORP", "multiplier": 1, "quantity": "10",
        "price": "40.5", "principal": "405", "fees": Decimal(0),
        "amount": "405", "where": "w",
    }
    base.update(overrides)
    return build_trade(**base)


# --------------------------------------------------------------------------
# 符号约定
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", BUY_ACTIONS)
def test_buys_are_negative(action: str) -> None:
    """**买入的净额为负**（现金流出）。"""
    trade = _trade(action=action, amount="405")

    assert trade.amount == "-405"


@pytest.mark.parametrize("action", ["Sell", "Sell to Open", "Sell to Close"])
def test_sells_are_positive(action: str) -> None:
    """卖出为正（现金流入）。"""
    trade = _trade(action=action, amount="405")

    assert trade.amount == "405"


def test_the_three_buy_actions_are_recognized() -> None:
    """三种买入动作都算买入。"""
    assert BUY_ACTIONS == ("Buy", "Buy to Open", "Buy to Close")


# --------------------------------------------------------------------------
# 本金勾稽
# --------------------------------------------------------------------------


def test_principal_must_equal_quantity_times_price() -> None:
    """本金 = 数量 × 价格 × 乘数。"""
    assert _trade(quantity="10", price="40.5", principal="405").quantity == "10"


def test_principal_mismatch_is_rejected() -> None:
    """本金明显不符必须报错。"""
    with pytest.raises(IngestError) as raised:
        _trade(principal="500", amount="500")

    assert "Principal 勾稽不符" in str(raised.value)


def test_rounding_within_tolerance_is_accepted() -> None:
    """**显示舍入在容差内被接受**（0.011）。"""
    # 3 × 33.3333 = 99.9999，显示为 100.00 → 差 0.0001。
    trade = _trade(quantity="3", price="33.3333", principal="100.00", amount="100.00")
    assert trade.quantity == "3"


def test_difference_just_over_tolerance_is_rejected() -> None:
    """**刚好超出容差即报错**——容差是边界而不是"差不多就行"。"""
    # 差 0.02 > 0.011。
    with pytest.raises(IngestError, match="Principal 勾稽不符"):
        _trade(quantity="1", price="100", principal="100.02", amount="100.02")


def test_tolerance_value_is_documented() -> None:
    """容差是 0.011（小于一分半，足以吸收舍入而拦得住真实错误）。"""
    assert RECONCILIATION_TOLERANCE == Decimal("0.011")


# --------------------------------------------------------------------------
# 净额勾稽
# --------------------------------------------------------------------------


def test_buy_net_is_principal_plus_fees() -> None:
    """买入：净额 = 本金 + 费用。"""
    trade = _trade(fees=Decimal("1.25"), amount="406.25")

    assert trade.fees == "1.25"
    assert trade.amount == "-406.25"


def test_sell_net_is_principal_minus_fees() -> None:
    """卖出：净额 = 本金 − 费用。"""
    trade = _trade(
        action="Sell", fees=Decimal("1.25"), amount="403.75"
    )

    assert trade.amount == "403.75"


def test_sell_net_mismatch_is_rejected() -> None:
    """卖出净额不符必须报错。"""
    with pytest.raises(IngestError) as raised:
        _trade(action="Sell", fees=Decimal("1.25"), amount="405")

    assert "Total Amount 勾稽不符" in str(raised.value)


def test_buy_net_mismatch_is_rejected() -> None:
    """买入净额不符必须报错。"""
    with pytest.raises(IngestError, match="Total Amount 勾稽不符"):
        _trade(fees=Decimal("1.25"), amount="405")


def test_zero_fees_net_equals_principal() -> None:
    """零费用时净额等于本金。"""
    trade = _trade(fees=Decimal(0), amount="405")
    assert trade.amount == "-405"


# --------------------------------------------------------------------------
# 期权乘数
# --------------------------------------------------------------------------


def test_option_multiplier_scales_the_principal() -> None:
    """**期权乘数是 100**：2 张 × $3 × 100 = $600。"""
    trade = _trade(
        symbol="INTC 03/19/2027 3.00 C", multiplier=100,
        quantity="2", price="3", principal="600", amount="600",
    )

    assert trade.quantity == "2"


def test_option_principal_without_the_multiplier_is_rejected() -> None:
    """忘掉乘数会让本金差 100 倍，勾稽立刻报错。"""
    with pytest.raises(IngestError, match="Principal 勾稽不符"):
        _trade(
            symbol="INTC 03/19/2027 3.00 C", multiplier=1,
            quantity="2", price="3", principal="600", amount="600",
        )


# --------------------------------------------------------------------------
# 输出格式
# --------------------------------------------------------------------------


def test_outputs_are_plain_decimals() -> None:
    """输出为无指数的十进制文本。**不引入科学计数法。**"""
    trade = _trade(quantity="0.0000001", price="0.0000001", principal="0", amount="0")

    for field in (trade.quantity, trade.price, trade.fees, trade.amount):
        assert "E" not in field.upper() or field.upper().startswith("-"), field


def test_fractional_quantity_is_preserved() -> None:
    """小数数量保留。"""
    trade = _trade(quantity="2.5", price="100", principal="250", amount="250")
    assert trade.quantity == "2.5"


def test_passthrough_fields() -> None:
    """日期、动作、符号与描述原样传递。"""
    trade = _trade(
        trade_date="2026-01-05", action="Buy to Open",
        symbol="INTC 03/19/2027 100.00 C", description="CALL INTEL CORP $100.00 EXP 03/19/27",
    )

    assert trade.trade_date == "2026-01-05"
    assert trade.action == "Buy to Open"
    assert trade.description == "CALL INTEL CORP $100.00 EXP 03/19/27"


def test_where_is_included_in_errors() -> None:
    """错误信息带定位，便于定位到具体哪一笔。"""
    with pytest.raises(IngestError) as raised:
        _trade(principal="500", amount="500", where="邮件 m1 第3笔")

    assert "邮件 m1 第3笔" in str(raised.value)
