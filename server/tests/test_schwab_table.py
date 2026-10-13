"""共享表格规则测试。

**列序是位置契约**：Schwab 的导出没有列名索引，第 7 列就是 ``Fees & Comm``。因此表头
必须**逐字符相等**，而不是"包含这些列"——顺序不同的两列会被映射到错误字段，
而金额看起来仍然正常。
"""

from __future__ import annotations

import pytest

from lyra.domain.ledger.errors import IngestError
from lyra.infrastructure.imports.schwab_table import (
    FIELD_COUNT,
    SCHWAB_TRANSACTION_HEADERS,
    assert_schwab_transaction_headers,
    create_schwab_unknown_headers_error,
    is_schwab_transaction_headers,
    map_schwab_transaction_row,
)


def _row() -> tuple[str, ...]:
    """一行合法数据。"""
    return ("01/05/2026", "Buy", "INTC", "INTEL CORP", "10", "$40.5", "", "-$405")


# --------------------------------------------------------------------------
# 表头
# --------------------------------------------------------------------------


def test_headers_are_the_eight_expected_columns() -> None:
    """表头是八个固定列，顺序固定。"""
    assert SCHWAB_TRANSACTION_HEADERS == (
        "Date", "Action", "Symbol", "Description",
        "Quantity", "Price", "Fees & Comm", "Amount",
    )
    assert FIELD_COUNT == 8


def test_exact_headers_are_accepted() -> None:
    """逐项相等即通过。"""
    assert is_schwab_transaction_headers(list(SCHWAB_TRANSACTION_HEADERS)) is True
    assert is_schwab_transaction_headers(SCHWAB_TRANSACTION_HEADERS) is True


def test_reordered_headers_are_rejected() -> None:
    """**顺序不同必须拒绝。**

    位置契约下，顺序不同会把两列映射到错误字段，而金额看起来仍然正常。
    """
    reordered = list(SCHWAB_TRANSACTION_HEADERS)
    reordered[4], reordered[5] = reordered[5], reordered[4]

    assert is_schwab_transaction_headers(reordered) is False


def test_subset_headers_are_rejected() -> None:
    """只含部分列同样拒绝。"""
    assert is_schwab_transaction_headers(list(SCHWAB_TRANSACTION_HEADERS[:7])) is False


def test_renamed_header_is_rejected() -> None:
    """列名改了就是另一张表。"""
    renamed = list(SCHWAB_TRANSACTION_HEADERS)
    renamed[6] = "Fees"

    assert is_schwab_transaction_headers(renamed) is False


@pytest.mark.parametrize("value", [None, "Date,Action", 5, {"Date": 1}])
def test_non_sequence_headers_are_rejected(value: object) -> None:
    """非序列的表头一律拒绝。"""
    assert is_schwab_transaction_headers(value) is False


def test_assertion_reports_expected_and_actual() -> None:
    """**错误里同时给出期望与实际**，否则用户只能猜是哪一列不对。"""
    with pytest.raises(IngestError) as raised:
        assert_schwab_transaction_headers(["Date", "Action"], "a.csv")

    message = str(raised.value)
    assert "期望" in message and "实际" in message
    assert "a.csv" in message


def test_assertion_accepts_valid_headers() -> None:
    """合法表头不报错。"""
    assert_schwab_transaction_headers(list(SCHWAB_TRANSACTION_HEADERS), "a.csv")


# --------------------------------------------------------------------------
# 行映射
# --------------------------------------------------------------------------


def test_row_maps_by_position() -> None:
    """按位置映射到领域字段。"""
    mapped = map_schwab_transaction_row(_row(), "w")

    assert mapped.date == "01/05/2026"
    assert mapped.action == "Buy"
    assert mapped.symbol == "INTC"
    assert mapped.description == "INTEL CORP"
    assert mapped.quantity == "10"
    assert mapped.price == "$40.5"
    assert mapped.fees == ""
    assert mapped.amount == "-$405"


def test_row_values_are_not_normalized() -> None:
    """**映射不做任何规范化**：规范化发生在领域标准化阶段。

    在这里顺手 trim 或补零会让"原始字段"不再原始，而下游的哈希基于它们计算。
    """
    mapped = map_schwab_transaction_row(
        (" 01/05/2026 ", "Buy", "INTC", "X", " 10 ", "$40.5", "", "-$405"), "w"
    )

    assert mapped.date == " 01/05/2026 "


@pytest.mark.parametrize("length", [7, 9, 0])
def test_wrong_column_count_is_rejected(length: int) -> None:
    """**列数必须恰好八。**

    少一列会让后面的列整体前移——金额被读成费用，而两者都"看起来像金额"。
    """
    with pytest.raises(IngestError, match="八个文本字段"):
        map_schwab_transaction_row(tuple(["x"] * length), "w")


def test_row_error_includes_where() -> None:
    """错误带定位。"""
    with pytest.raises(IngestError) as raised:
        map_schwab_transaction_row(("a",), "a.csv 第3行")

    assert "a.csv 第3行" in str(raised.value)


# --------------------------------------------------------------------------
# 未找到交易表
# --------------------------------------------------------------------------


def test_unknown_headers_error_lists_the_worksheets() -> None:
    """**列出实际检查过的工作表名。**

    否则用户只能猜自己是不是选错了文件。
    """
    error = create_schwab_unknown_headers_error(("Sheet1", "Summary"))

    assert "Sheet1" in str(error) and "Summary" in str(error)
    assert "没有 Schwab Transactions 表" in str(error)


def test_unknown_headers_error_handles_no_worksheets() -> None:
    """空工作簿时列出空列表，而不是崩溃。"""
    assert "[]" in str(create_schwab_unknown_headers_error(()))
