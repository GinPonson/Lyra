"""邮件成交 → 原始字段映射测试。

**必须与 CSV 解析器产出的字段逐字符一致**，否则同一笔交易经邮件导入与经 CSV 导入
会得到不同的交易哈希，跨来源去重失效——**用户会看到同一笔交易出现两次**。

三处格式约定各自都会破坏这个一致性：

- ``Date`` 的零填充；
- ``Fees & Comm`` 为零时留空（而不是 ``$0``）；
- ``Amount`` 的符号位在 ``$`` 之前。
"""

from __future__ import annotations

import pytest

from lyra.domain.gmail.econfirm import EmailTrade, email_trade_to_raw_fields
from lyra.infrastructure.imports.schwab_csv import (
    SCHWAB_TRANSACTION_HEADERS,
    parse_schwab_csv,
)


def _trade(**overrides) -> EmailTrade:
    base = {
        "trade_date": "2026-01-05", "action": "Buy", "symbol": "INTC",
        "description": "INTEL CORP", "quantity": "10", "price": "40.5",
        "fees": "0", "amount": "-405",
    }
    base.update(overrides)
    return EmailTrade(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 与 CSV 解析器的一致性
# --------------------------------------------------------------------------


def test_email_and_csv_produce_identical_domain_fields() -> None:
    """**端到端对照**：同一笔交易经 CSV 解析与经邮件转换得到完全相同的领域字段。

    这是跨来源去重的真正依据——字段不同的任何一个字符都会让两条路径算出不同的
    交易哈希，**用户就会看到同一笔交易出现两次**。

    这里不是"字段名清单比对"，而是真的把等价 CSV 文本喂进 CSV 解析器再逐字段比较。
    """
    trade = _trade()
    from_email = email_trade_to_raw_fields(trade)

    csv_text = (
        b"Date,Action,Symbol,Description,Quantity,Price,Fees & Comm,Amount\n"
        b"01/05/2026,Buy,INTC,INTEL CORP,10,$40.5,,-$405\n"
    )
    batch = parse_schwab_csv(
        csv_text, file_name="Individual_XXX276_Transactions_20260105.csv",
        source_hash="h", source_reference="a.csv"
    )
    from_csv = batch.rows[0]

    assert from_email == from_csv


def test_domain_field_names_are_python_style_not_csv_headers() -> None:
    """领域对象用 Python 命名，CSV 表头名只在适配器里出现。"""
    fields = email_trade_to_raw_fields(_trade())

    assert [name for name in vars(fields)] == [
        "date", "action", "symbol", "description", "quantity", "price", "fees", "amount",
    ]
    # CSV 表头常量仍存在，供适配器使用。
    assert set(SCHWAB_TRANSACTION_HEADERS) == {
        "Date", "Action", "Symbol", "Description", "Quantity", "Price",
        "Fees & Comm", "Amount",
    }


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------


def test_date_is_zero_padded_month_day_year() -> None:
    """日期格式为 ``MM/DD/YYYY`` 且**零填充**（与 Schwab 展示格式一致）。"""
    assert email_trade_to_raw_fields(_trade(trade_date="2026-01-05")).date == (
        "01/05/2026"
    )


@pytest.mark.parametrize("value", ["01/05/2026", "", "2026-01", "2026"])
def test_dates_that_cannot_be_split_are_rejected(value: str) -> None:
    """无法拆成三段的日期必须报错，而不是产出畸形日期。"""
    with pytest.raises(ValueError, match="无法格式化"):
        email_trade_to_raw_fields(_trade(trade_date=value))


def test_partially_padded_dates_pass_through() -> None:
    """**只校验"能否拆成三段"，不校验补零。**

    这与参考实现逐字一致：``2026-1-5`` 会产出 ``1/5/2026``。实际调用方是 eConfirm
    解析器，它产出的始终是补零的 ISO 日期，因此这条路径不会在正常流程中触发——
    但把它写成"拒绝"会让本函数比参考实现更严格，**在迁移中是行为偏离**。
    """
    assert email_trade_to_raw_fields(_trade(trade_date="2026-1-5")).date == "1/5/2026"


# --------------------------------------------------------------------------
# 费用为零时留空
# --------------------------------------------------------------------------


def test_zero_fees_render_as_empty() -> None:
    """**零费用留空**：写 ``$0`` 会改变行哈希，破坏跨来源一致性。"""
    assert email_trade_to_raw_fields(_trade(fees="0")).fees == ""
    assert email_trade_to_raw_fields(_trade(fees="0.00")).fees == ""
    assert email_trade_to_raw_fields(_trade(fees="-0")).fees == ""


def test_nonzero_fees_render_with_a_dollar_sign() -> None:
    """非零费用带美元符号。"""
    assert email_trade_to_raw_fields(_trade(fees="1.25")).fees == "$1.25"


# --------------------------------------------------------------------------
# 金额符号位
# --------------------------------------------------------------------------


def test_negative_amount_puts_the_sign_before_the_dollar_sign() -> None:
    """**符号位在 ``$`` 之前**：``-$405``，不是 ``$-405``。"""
    assert email_trade_to_raw_fields(_trade(amount="-405")).amount == "-$405"


def test_positive_amount_has_no_sign() -> None:
    """正数不加 ``+``。"""
    assert email_trade_to_raw_fields(_trade(amount="403.75")).amount == "$403.75"


def test_zero_amount_is_unsigned() -> None:
    """零金额不带负号（``-0`` 不是负数）。"""
    assert email_trade_to_raw_fields(_trade(amount="0")).amount == "$0"


# --------------------------------------------------------------------------
# 数量与价格
# --------------------------------------------------------------------------


def test_quantity_and_price_are_plain_decimals() -> None:
    """数量与价格是无指数的十进制文本。"""
    fields = email_trade_to_raw_fields(_trade(quantity="10", price="40.5"))

    assert fields.quantity == "10"
    assert fields.price == "$40.5"


def test_price_has_no_exponent_for_small_values() -> None:
    """极小价格不出现科学计数法——``1E-7`` 会让 CSV 解析器读不到。"""
    fields = email_trade_to_raw_fields(_trade(price="0.0000001"))

    assert fields.price == "$0.0000001"
    assert "E" not in fields.price


def test_passthrough_fields_are_unchanged() -> None:
    """动作、符号与描述原样传递。"""
    fields = email_trade_to_raw_fields(_trade(action="Sell to Close", symbol="INTC 270319C00100000"))

    assert fields.action == "Sell to Close"
    assert fields.symbol == "INTC 270319C00100000"
    assert fields.description == "INTEL CORP"
