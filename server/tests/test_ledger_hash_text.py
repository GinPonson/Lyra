"""业务哈希的十进制文本（``decimal_hash_text``）测试。

哈希把**源文本标度**当契约：同一份 Schwab 文件在迁移前后必须算出同一个
``transaction_hash``，否则重复导入会产生重复交易。

科学计数法分支最容易出错：Python 的 ``str(Decimal)`` 给出 ``1.5E+5``，而 TypeScript
的 ``value.toString()``（decimal.js）给出 ``150000``。期望值取 TS 侧的真实输出，
即 decimal.js 默认 ``toString()``（``toExpNeg = -7``、``toExpPos = 21``）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from lyra.domain.ledger.transactions import decimal_hash_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 定点：小数位取自清洗后的源文本，`$5.00` 不会被缩成 `5`。
        ("$5.00", "5.00"),
        ("1,234.50", "1234.50"),
        ("1.000000", "1.000000"),
        ("0", "0"),
        # 负零归一到正零（decimal.js 不保留负零）。
        ("-0.00", "0.00"),
        ("-0.000", "0.000"),
        # 科学计数法：与 decimal.js `toString()` 逐字符一致。
        ("1.5e5", "150000"),
        ("1.5E+5", "150000"),
        ("1E5", "100000"),
        ("-1.5e3", "-1500"),
        ("1e-7", "1e-7"),
        ("  $1.5e5  ", "150000"),
    ],
)
def test_decimal_hash_text_matches_typescript(raw: str, expected: str) -> None:
    """逐例与 TypeScript ``value.toString()`` 对齐。"""
    assert decimal_hash_text(raw) == expected


def test_empty_text_is_null() -> None:
    """空文本统一为账本 ``None``。"""
    assert decimal_hash_text("") is None
    assert decimal_hash_text("   ") is None


def test_scientific_notation_regression() -> None:
    """**回归**：旧实现 ``str(Decimal("1.5e5"))`` 给出 ``1.5E+5``，与 TS 不同。

    保留这三条断言，是为了让"改回 ``str()``"这种回退立刻被测试拦住——
    并且说明为什么不能只做定点渲染：decimal.js 在 ``1e-7`` 这类量级上仍然用指数形式。
    """
    assert str(Decimal("1.5e5")) == "1.5E+5"
    assert decimal_hash_text("1.5e5") == "150000"
    assert decimal_hash_text("1e-7") == "1e-7"
