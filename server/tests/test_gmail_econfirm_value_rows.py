"""成交金额表解析测试。

Totals 分支做**三处勾稽**：逐腿费用之和、数量之和、金额绝对值之和都必须等于 Totals
给出的值。任一处不符即报错——**不平均分摊、不按比例缩放**：分摊出来的数字在账本里
看起来完全正常，但它对应的是从未真实发生过的成交。
"""

from __future__ import annotations

import pytest

from lyra.domain.gmail.econfirm import parse_value_rows
from lyra.domain.ledger.errors import IngestError


def _rows(values, *, multiplier: int = 1, action: str = "Buy", symbol: str = "INTC"):
    """跑一次解析。"""
    return parse_value_rows(
        values=tuple(values), where="w", trade_date="2026-01-05", action=action,
        symbol=symbol, description="INTEL CORP", multiplier=multiplier,
    )


# 零费用腿的尾部：`n/a:` + 金额 + `n/a` + 净额。
def _zero_fee_tail(amount: str) -> list[str]:
    """构造零费用尾部。"""
    return ["n/a:", "$0.00", "n/a", amount]


# --------------------------------------------------------------------------
# 单笔
# --------------------------------------------------------------------------


def test_single_row_with_a_bare_net_amount() -> None:
    """尾部只有一个金额时零费用。"""
    rows = _rows(["10", "40.5", "405", "$405"])

    assert len(rows) == 1
    assert rows[0].fees == "0"
    assert rows[0].amount == "-405"


def test_single_row_with_an_na_pair() -> None:
    """``n/a`` + 金额的两项形态。"""
    rows = _rows(["10", "40.5", "405", "n/a", "$405"])

    assert len(rows) == 1
    assert rows[0].fees == "0"


def test_single_row_with_an_na_block() -> None:
    """``n/a:`` 四项形态（费用必须为 0）。"""
    rows = _rows(["10", "40.5", "405", *_zero_fee_tail("$405")])

    assert rows[0].amount == "-405"


def test_single_row_falls_back_to_labeled_fees() -> None:
    """都不是紧凑形态时落到带标签的费用表。"""
    rows = _rows([
        "10", "40.5", "405",
        "commission:", "$1.25", "industry fee:", "$0.03", "total:", "$1.28", "$406.28",
    ])

    assert rows[0].fees == "1.28"
    assert rows[0].amount == "-406.28"


def test_too_few_values_is_rejected() -> None:
    """少于四项无法识别。"""
    with pytest.raises(IngestError, match="无法识别成交金额结构"):
        _rows(["10", "40.5", "405"])


# --------------------------------------------------------------------------
# Totals：无分隔符（每四条一腿）
# --------------------------------------------------------------------------


def test_totals_without_separators_splits_into_fours() -> None:
    """无分隔符时每四条一腿。"""
    rows = _rows([
        "2", "40", "80", "$80",
        "3", "40", "120", "$120",
        "Totals", "5", "x", "y", "$0.00", "$200",
    ])

    assert len(rows) == 2
    assert [row.quantity for row in rows] == ["2", "3"]
    assert [row.amount for row in rows] == ["-80", "-120"]


def test_totals_without_separators_requires_a_multiple_of_four() -> None:
    """不是四的倍数即报错。"""
    with pytest.raises(IngestError, match="无法识别多价格成交结构"):
        _rows([
            "2", "40", "80", "$80", "3",
            "Totals", "5", "x", "y", "$0.00", "$200",
        ])


# --------------------------------------------------------------------------
# Totals：有分隔符（逐腿）
# --------------------------------------------------------------------------


def test_totals_with_separators_splits_by_leg() -> None:
    """``for the above:`` 分隔成交腿。"""
    rows = _rows([
        "2", "40", "80", *_zero_fee_tail("$80"),
        "for the above:",
        "3", "40", "120", *_zero_fee_tail("$120"),
        "Totals", "5", "x", "y", "$0.00", "$200",
    ])

    assert [row.quantity for row in rows] == ["2", "3"]
    assert [row.amount for row in rows] == ["-80", "-120"]


def test_a_trailing_separator_before_totals_is_not_an_empty_leg() -> None:
    """**Totals 前最后一个分隔符不代表空腿。**

    新版模板在最后一腿与 Totals 之间也保留分隔标记；不处理它会多出一"腿"，
    然后因字段不足而报错。
    """
    rows = _rows([
        "2", "40", "80", *_zero_fee_tail("$80"),
        "for the above:",
        "3", "40", "120", *_zero_fee_tail("$120"),
        "for the above:",
        "Totals", "5", "x", "y", "$0.00", "$200",
    ])

    assert len(rows) == 2


def test_the_disclosure_guard_keeps_multi_leg_data_intact() -> None:
    """**对照验证：同一段数据，有无 Totals 决定截断行为。**

    - 无 Totals：``for the above:`` 是免责声明，在它处截断（只剩第一腿）；
    - 有 Totals：同一文本是**腿分隔符**，不得截断——否则会把 Totals 一并切掉，
      退回单笔解析并**静默丢掉后续腿**。

    这是参考实现里 ``!values.includes("Totals")`` 那条守卫的直接验证。
    """
    legs = [
        "2", "40", "80", *_zero_fee_tail("$80"),
        "for the above:",
        "3", "40", "120", *_zero_fee_tail("$120"),
    ]

    # 无 Totals：截断在免责声明处，只解析第一腿。
    without_totals = _rows([*legs, "for the above: see disclosures"])
    assert len(without_totals) == 1
    assert without_totals[0].quantity == "2"

    # 有 Totals：不截断，两腿都解析出来。
    with_totals = _rows([*legs, "Totals", "5", "x", "y", "$0.00", "$200"])
    assert len(with_totals) == 2
    assert [row.quantity for row in with_totals] == ["2", "3"]


def test_disclosure_truncates_when_there_is_no_totals() -> None:
    """没有 Totals 时免责声明确实截断。"""
    rows = _rows([
        "10", "40.5", "405", "$405",
        "for the above: see disclosures",
        "unrelated trailing text",
    ])

    assert len(rows) == 1
    assert rows[0].quantity == "10"


# --------------------------------------------------------------------------
# 三处勾稽
# --------------------------------------------------------------------------


def test_fee_mismatch_is_rejected() -> None:
    """**逐腿费用之和不等于 Totals 即报错。**"""
    with pytest.raises(IngestError, match="费用无法可靠分摊"):
        _rows([
            "2", "40", "80", *_zero_fee_tail("$80"),
            "for the above:",
            "3", "40", "120", *_zero_fee_tail("$120"),
            "Totals", "5", "x", "y", "$9.99", "$200",
        ])


def test_quantity_mismatch_is_rejected() -> None:
    """数量之和不等于 Totals 即报错。"""
    with pytest.raises(IngestError, match="数量与 Totals 不一致"):
        _rows([
            "2", "40", "80", *_zero_fee_tail("$80"),
            "for the above:",
            "3", "40", "120", *_zero_fee_tail("$120"),
            "Totals", "99", "x", "y", "$0.00", "$200",
        ])


def test_amount_mismatch_is_rejected() -> None:
    """**金额绝对值之和不等于 Totals 即报错。**"""
    with pytest.raises(IngestError, match="金额与 Totals 不一致"):
        _rows([
            "2", "40", "80", *_zero_fee_tail("$80"),
            "for the above:",
            "3", "40", "120", *_zero_fee_tail("$120"),
            "Totals", "5", "x", "y", "$0.00", "$999",
        ])


def test_amount_reconciliation_uses_absolute_values() -> None:
    """**金额用绝对值相加**：买入为负、卖出为正，Totals 给的是规模。"""
    rows = _rows([
        "2", "40", "80", *_zero_fee_tail("$80"),
        "for the above:",
        "3", "40", "120", *_zero_fee_tail("$120"),
        "Totals", "5", "x", "y", "$0.00", "$200",
    ], action="Sell")

    # 卖出为正，但 Totals 仍然勾稽通过。
    assert [row.amount for row in rows] == ["80", "120"]


def test_totals_block_too_short_is_rejected() -> None:
    """Totals 后少于四项无法识别。"""
    with pytest.raises(IngestError, match="无法识别多价格成交结构"):
        _rows(["2", "40", "80", "$80", "Totals", "2", "$80"])


def test_short_leg_with_separators_is_rejected() -> None:
    """某腿字段不足时报错并列出该腿。"""
    with pytest.raises(IngestError, match="单腿字段不足"):
        _rows([
            "2", "40",
            "for the above:",
            "3", "40", "120", *_zero_fee_tail("$120"),
            "Totals", "5", "x", "y", "$0.00", "$200",
        ])


# --------------------------------------------------------------------------
# 期权
# --------------------------------------------------------------------------


def test_option_multiplier_applies_to_every_leg() -> None:
    """期权乘数作用于每一腿。"""
    rows = _rows([
        "1", "3", "300", *_zero_fee_tail("$300"),
        "for the above:",
        "2", "3", "600", *_zero_fee_tail("$600"),
        "Totals", "3", "x", "y", "$0.00", "$900",
    ], multiplier=100, symbol="INTC 03/19/2027 3.00 C")

    assert [row.quantity for row in rows] == ["1", "2"]
    assert [row.amount for row in rows] == ["-300", "-600"]
