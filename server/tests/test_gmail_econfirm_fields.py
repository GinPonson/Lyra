"""eConfirm 字段解析测试。

最要紧的一条：**期权描述会被重建为规范形式。**

Schwab 的描述格式在不同批次间会变，而描述进入**交易哈希**——不规范化会让同一笔交易
在两次导入中算出不同哈希，跨来源与跨次去重随之失效。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from lyra.domain.gmail.econfirm import (
    SYMBOL_SPLIT_PATTERN,
    field,
    normalize_description,
    parse_email_decimal,
    parse_trade_date,
    unwrap_symbol,
)
from lyra.domain.ledger.errors import IngestError


def _block(**fields: str) -> str:
    """构造一个交易块文本。"""
    return "\n\n".join(f"{name}:\n\n{value}" for name, value in fields.items())


# --------------------------------------------------------------------------
# 字段
# --------------------------------------------------------------------------


def test_reads_a_single_line_field() -> None:
    """读取「标签:」独占一行、值在下一行的字段。"""
    block = _block(**{"Security Description": "INTEL CORP"})

    assert field(block, "Security Description", "w") == "INTEL CORP"


def test_field_is_case_insensitive() -> None:
    """字段名大小写不敏感。"""
    block = _block(**{"Trade Date": "1/5/2026"})
    assert field(block, "trade date", "w") == "1/5/2026"


def test_field_requires_a_line_of_its_own() -> None:
    """**「标签:」必须独占一行。**

    用"标签后紧跟冒号"匹配会在正文里出现同名普通词时误命中——例如描述里提到
    "Action: 见下"这种句子。
    """
    block = "Some text mentioning Action: inline here\n\nOther:\n\nvalue"

    with pytest.raises(IngestError, match="缺少字段 'Action'"):
        field(block, "Action", "w")


def test_missing_field_reports_the_where() -> None:
    """缺失时带上定位信息。"""
    with pytest.raises(IngestError) as raised:
        field("", "Action", "邮件 m1 第2笔")

    assert "邮件 m1 第2笔" in str(raised.value)


def test_field_strips_whitespace() -> None:
    """值两端空白被去除。"""
    block = "Action:\n\n   Sell to Close   \n"
    assert field(block, "Action", "w") == "Sell to Close"


# --------------------------------------------------------------------------
# 符号
# --------------------------------------------------------------------------


def test_unwraps_a_markdown_link() -> None:
    """**正文里的符号可能是 Markdown 链接。**

    把整串 ``[INTC](https://…)`` 当成符号会让下游解析出错误的代码。
    """
    assert unwrap_symbol("[INTC](https://example.com/x)") == "INTC"
    assert unwrap_symbol("[INTC 03/19/2027 100.00 C](https://x)") == (
        "INTC 03/19/2027 100.00 C"
    )


def test_plain_symbol_passes_through() -> None:
    """普通符号原样返回。"""
    assert unwrap_symbol("INTC") == "INTC"
    assert unwrap_symbol("  INTC  ") == "INTC"


def test_malformed_markdown_is_left_alone() -> None:
    """不完整的链接语法不做猜测。"""
    assert unwrap_symbol("[INTC") == "[INTC"
    assert unwrap_symbol("INTC]") == "INTC]"


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1/5/2026", "2026-01-05"),
        ("12/31/2026", "2026-12-31"),
        ("01/05/2026", "2026-01-05"),
        # **两位数年份按 20xx 解释**（Schwab 的展示格式）。
        ("1/5/26", "2026-01-05"),
        ("1/5/99", "2099-01-05"),
    ],
)
def test_parses_trade_dates(raw: str, expected: str) -> None:
    """解析并补零。"""
    assert parse_trade_date(raw, "w") == expected


@pytest.mark.parametrize(
    "raw",
    [
        "2026-01-05",    # 不是 M/D/YYYY
        "1/5",           # 缺年份
        "1/5/2026/9",    # 多一段
        "2/30/2026",     # **格式合法但日期不存在**
        "13/1/2026",     # 月份越界
        "0/5/2026",
        "abc",
        "",
    ],
)
def test_invalid_trade_dates_are_rejected(raw: str) -> None:
    """**不存在的日期必须拒绝**（``2/30`` 这种）。"""
    with pytest.raises(IngestError, match="无法解析交易日期"):
        parse_trade_date(raw, "w")


# --------------------------------------------------------------------------
# 数值
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1,234.56", Decimal("1234.56")),
        ("-$405", Decimal(-405)),
        ("100", Decimal(100)),
        ("$0.005", Decimal("0.005")),
        ("  $2.50  ", Decimal("2.50")),
    ],
)
def test_parses_decimals(raw: str, expected: Decimal) -> None:
    """剥掉货币符号与千分位；**负号保留**。"""
    assert parse_email_decimal(raw, "w") == expected


@pytest.mark.parametrize("raw", ["", "abc", "$", "1.2.3"])
def test_invalid_decimals_are_rejected(raw: str) -> None:
    """无法解析的数值报错。"""
    with pytest.raises(IngestError, match="无法解析数值"):
        parse_email_decimal(raw, "w")


# --------------------------------------------------------------------------
# 描述规范化
# --------------------------------------------------------------------------


def test_stock_description_is_kept_as_is() -> None:
    """正股描述原样保留。"""
    assert normalize_description("INTC", "  INTEL CORP  ") == "INTEL CORP"


def test_option_description_is_rebuilt() -> None:
    """**期权描述重建为规范形式**：``CALL|PUT 公司 $行权价 EXP MM/DD/YY``。"""
    assert normalize_description(
        "INTC 03/19/2027 100.00 C", "INTEL CORP 03/19/2027 $100.00 Call"
    ) == "CALL INTEL CORP $100.00 EXP 03/19/27"


def test_put_description_uses_put() -> None:
    """看跌期权用 PUT。"""
    assert normalize_description(
        "AMD 01/15/2027 2.5 P", "ADVANCED MICRO 01/15/2027 $2.50 Put"
    ) == "PUT ADVANCED MICRO $2.5 EXP 01/15/27"


def test_option_description_is_stable_across_vendor_formats() -> None:
    """**厂商格式不同但重建结果相同。**

    这正是规范化的目的：描述进入交易哈希，格式漂移会让去重失效。
    """
    first = normalize_description(
        "INTC 03/19/2027 100.00 C", "INTEL CORP 03/19/2027 $100.00 Call"
    )
    second = normalize_description(
        "INTC 03/19/2027 100.00 C", "INTEL CORP  03/19/2027  $100.00  Call"
    )

    assert first == second


def test_description_without_the_vendor_suffix_still_rebuilds() -> None:
    """没有厂商后缀时公司名原样使用。"""
    assert normalize_description(
        "INTC 03/19/2027 100.00 C", "INTEL CORP"
    ) == "CALL INTEL CORP $100.00 EXP 03/19/27"


# --------------------------------------------------------------------------
# 分块
# --------------------------------------------------------------------------


def test_symbol_split_pattern_matches_a_standalone_label() -> None:
    """分块模式匹配独占一行的 ``Symbol:``。"""
    body = "前言\n\nSymbol:\n\nINTC\n\nAction:\n\nBuy"

    parts = SYMBOL_SPLIT_PATTERN.split(body)

    assert len(parts) == 2
    assert "INTC" in parts[1]


def test_body_without_a_symbol_block_yields_one_part() -> None:
    """没有 ``Symbol:`` 块时只有一段（调用方据此报"正文中没有交易块"）。"""
    assert len(SYMBOL_SPLIT_PATTERN.split("just some text")) == 1


def test_symbol_inside_a_line_is_not_a_separator() -> None:
    """行内的 ``Symbol:`` 不是分隔符——必须是独占一行。"""
    body = "see Symbol: INTC here\nmore text"
    assert len(SYMBOL_SPLIT_PATTERN.split(body)) == 1
