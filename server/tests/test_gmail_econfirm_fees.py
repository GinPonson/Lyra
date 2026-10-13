"""费用区域解析测试。

两处"宁可失败也不猜"的设计：

1. **分项费用必须等于 Total**——采信哪一边都是在猜，而费用会进入交易哈希与已实现损益；
2. **尾部不得有多余字段**——模板新增字段时静默忽略会让解析结果悄悄漏掉信息。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from lyra.domain.gmail.econfirm import MONEY_PATTERN, is_money, parse_labeled_fees
from lyra.domain.ledger.errors import IngestError

# --------------------------------------------------------------------------
# 金额判定
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["$0", "$1.25", "$1,234.56", "-$405", "-$1,234.56", "$0.00"]
)
def test_valid_money(value: str) -> None:
    """合法的美元文本。"""
    assert is_money(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "1.25",      # **没有货币符号**：正文里裸数字随处可见
        "$", "abc", "", "1,234", "$1.2.3", "$-1", None, 5,
    ],
)
def test_invalid_money(value: object) -> None:
    """**不带 ``$`` 的数字不算金额。**

    正文里日期、数量、行号都是裸数字；把它们当金额会让解析器在错误的字段上对齐。
    """
    assert is_money(value) is False


def test_money_pattern_requires_the_dollar_sign() -> None:
    """模式本身要求 ``$``。"""
    assert MONEY_PATTERN.fullmatch("$1") is not None
    assert MONEY_PATTERN.fullmatch("1") is None


# --------------------------------------------------------------------------
# 模板一：零费用行
# --------------------------------------------------------------------------


def test_na_zero_fee_row() -> None:
    """``["n/a:", money, "n/a", money]`` → 费用 0，净额取第四项。"""
    fees, amount = parse_labeled_fees(("n/a:", "$0.00", "n/a", "$405.00"), "w")

    assert fees == Decimal("0.00")
    assert amount == "$405.00"


def test_na_row_with_nonzero_fee_is_rejected() -> None:
    """**N/A 行声明的费用必须是 0。**

    非零说明模板与我们对它的理解不一致——此时采信金额会凭空多出一笔费用。
    """
    with pytest.raises(IngestError, match="N/A 费用必须为 0"):
        parse_labeled_fees(("n/a:", "$1.25", "n/a", "$405.00"), "w")


def test_na_row_requires_both_labels() -> None:
    """两处 ``n/a`` 标签缺一就落到完整模板路径（并因缺 Industry Fee 而失败）。"""
    with pytest.raises(IngestError):
        parse_labeled_fees(("n/a:", "$0.00", "$405.00"), "w")


# --------------------------------------------------------------------------
# 模板二：只有 Industry Fee
# --------------------------------------------------------------------------


def test_industry_fee_only() -> None:
    """``industry fee:`` 开头时佣金按 0 计。"""
    fees, amount = parse_labeled_fees(("industry fee:", "$0.03", "$405.03"), "w")

    assert fees == Decimal("0.03")
    assert amount == "$405.03"


def test_industry_only_tolerates_a_bare_label() -> None:
    """金额后可能跟一个裸标签。"""
    fees, amount = parse_labeled_fees(
        ("industry fee:", "$0.03", "industry fee", "$405.03"), "w"
    )

    assert fees == Decimal("0.03")
    assert amount == "$405.03"


# --------------------------------------------------------------------------
# 模板三：完整
# --------------------------------------------------------------------------


def test_full_template_sums_the_fee_parts() -> None:
    """``commission + industry fee``，并校验等于 Total。"""
    fees, amount = parse_labeled_fees(
        ("commission:", "$1.25", "industry fee:", "$0.03", "total:", "$1.28", "$406.28"),
        "w",
    )

    assert fees == Decimal("1.28")
    assert amount == "$406.28"


def test_full_template_tolerates_the_bare_commission_label() -> None:
    """金额后可能跟一个裸 ``commission`` 标签。"""
    fees, _ = parse_labeled_fees(
        ("commission:", "$1.25", "commission", "industry fee:", "$0.03",
         "total:", "$1.28", "$406.28"),
        "w",
    )

    assert fees == Decimal("1.28")


def test_bare_money_commission_is_accepted() -> None:
    """第一位就是金额时直接当佣金。"""
    fees, _ = parse_labeled_fees(
        ("$1.25", "industry fee:", "$0.03", "total:", "$1.28", "$406.28"), "w"
    )

    assert fees == Decimal("1.28")


def test_fee_parts_must_sum_to_the_total() -> None:
    """**分项费用与 Total 不符必须报错。**

    采信哪一边都是在猜，而费用会进入交易哈希与已实现损益。
    """
    with pytest.raises(IngestError, match="分项费用与 Total 不一致"):
        parse_labeled_fees(
            ("commission:", "$1.25", "industry fee:", "$0.03",
             "total:", "$9.99", "$406.28"),
            "w",
        )


# --------------------------------------------------------------------------
# 结构错误
# --------------------------------------------------------------------------


def test_missing_industry_fee_label_is_rejected() -> None:
    """完整模板缺少 Industry Fee 标签时报错。"""
    with pytest.raises(IngestError, match="缺少 Industry Fee 标签"):
        parse_labeled_fees(("commission:", "$1.25", "total:", "$1.25", "$406.25"), "w")


def test_missing_total_label_is_rejected() -> None:
    """缺少 Total 标签时报错。"""
    with pytest.raises(IngestError, match="缺少费用 Total 标签"):
        parse_labeled_fees(
            ("commission:", "$1.25", "industry fee:", "$0.03", "$406.28"), "w"
        )


def test_unrecognizable_leading_field_is_rejected() -> None:
    """首位既不是标签也不是金额 → 报错并列出实际内容。"""
    with pytest.raises(IngestError) as raised:
        parse_labeled_fees(("garbage", "$1.25"), "w")

    assert "无法识别 Commission 字段" in str(raised.value)


def test_missing_commission_amount_is_rejected() -> None:
    """``commission:`` 后没有金额时报错。"""
    with pytest.raises(IngestError, match="Commission 后缺少金额"):
        parse_labeled_fees(("commission:", "industry fee:", "$0.03"), "w")


def test_missing_industry_fee_amount_is_rejected() -> None:
    """``industry fee:`` 后没有金额时报错。"""
    with pytest.raises(IngestError, match="Industry Fee 后缺少金额"):
        parse_labeled_fees(("commission:", "$1.25", "industry fee:", "total:"), "w")


def test_trailing_unknown_fields_are_rejected() -> None:
    """**尾部多余字段必须报错。**

    模板新增字段时静默忽略会让解析结果**悄悄漏掉信息**——错误在很久之后才以
    "某笔交易金额不对"的形式浮现，而那时已无从追溯。
    """
    with pytest.raises(IngestError, match="费用表包含未知字段"):
        parse_labeled_fees(
            ("commission:", "$1.25", "industry fee:", "$0.03",
             "total:", "$1.28", "$406.28", "surprise"),
            "w",
        )


def test_trailing_unknown_fields_rejected_in_industry_only_too() -> None:
    """只有 Industry Fee 的模板同样拒绝多余字段。"""
    with pytest.raises(IngestError, match="费用表包含未知字段"):
        parse_labeled_fees(("industry fee:", "$0.03", "$405.03", "extra"), "w")


def test_where_is_included_in_errors() -> None:
    """错误信息带定位。"""
    with pytest.raises(IngestError) as raised:
        parse_labeled_fees(("garbage",), "邮件 m1 第2笔")

    assert "邮件 m1 第2笔" in str(raised.value)
