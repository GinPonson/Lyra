"""`parse_econfirm_body` 端到端测试。

正文被 ``Symbol:`` 分成若干块（第一段是前言），**每块对应一个证券**；一个块内的金额表
又可以含多笔成交（多价格成交），因此返回值是全部成交的拼接。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from lyra.domain.gmail.econfirm import (
    ADDITIONAL_INFORMATION_MARKER,
    GmailMessage,
    parse_econfirm,
    parse_econfirm_body,
    parse_value_rows,
)
from lyra.domain.ledger.errors import IngestError
from lyra.infrastructure.gmail.html_text import html_to_text


_MULTI_PRICE_HTML = (
    Path(__file__).parent / "fixtures" / "gmail_econfirm_charge_interest_multi_price.html"
).read_text()
_LABELED_FEES_HTML = (
    Path(__file__).parent / "fixtures" / "gmail_econfirm_charge_interest_labeled_fees.html"
).read_text()


def _block(
    *,
    symbol: str = "[INTC](https://example.com/x)",
    description: str = "INTEL CORP",
    action: str = "Purchase",
    position_type: str = "Margin",
    trade_date: str = "1/5/2026",
    values: tuple[str, ...] = ("10", "40.5", "405", "$405"),
    extra: str = "",
) -> str:
    """构造一个交易块。"""
    return (
        f"Symbol:\n\n{symbol}\n\n"
        f"Security Description:\n\n{description}\n\n"
        f"Action:\n\n{action}\n\n"
        f"Type:\n\n{position_type}\n\n"
        f"Trade Date:\n\n{trade_date}\n\n"
        f"Total Amount\n\n" + "\n\n".join(values) + extra
    )


def _body(*blocks: str) -> str:
    """把若干块拼成正文（首段是前言）。"""
    return "Schwab eConfirms\n\nAccount ending in 276\n\n" + "\n\n".join(blocks)


# --------------------------------------------------------------------------
# 正股
# --------------------------------------------------------------------------


def test_parses_a_stock_block() -> None:
    """正股块解析出动作、日期与金额。"""
    trades = parse_econfirm_body("m1", _body(_block()))

    assert len(trades) == 1
    trade = trades[0]
    assert trade.action == "Buy"
    assert trade.symbol == "INTC"
    assert trade.trade_date == "2026-01-05"
    assert trade.quantity == "10"
    assert trade.amount == "-405"


def test_sell_maps_for_stocks() -> None:
    """股票只看买卖方向。"""
    trades = parse_econfirm_body("m1", _body(_block(
        action="Sale", values=("10", "40.5", "405", "$405")
    )))

    assert trades[0].action == "Sell"
    assert trades[0].amount == "405"


def test_symbol_is_unwrapped_from_markdown() -> None:
    """**正文里的符号可能是 Markdown 链接。**"""
    trades = parse_econfirm_body("m1", _body(_block(symbol="[AMD](https://x)")))

    assert trades[0].symbol == "AMD"


def test_plain_symbol_works_too() -> None:
    """普通符号同样可用。"""
    assert parse_econfirm_body("m1", _body(_block(symbol="AMD")))[0].symbol == "AMD"


# --------------------------------------------------------------------------
# 期权
# --------------------------------------------------------------------------


def test_parses_an_option_block() -> None:
    """**期权动作由买卖与保证金/做空共同决定。**

    只看买卖会把"买入开仓"记成"买入平仓"，持仓数量随之算错。
    """
    trades = parse_econfirm_body("m1", _body(_block(
        symbol="INTC 03/19/2027 100.00 C",
        description="INTEL CORP 03/19/2027 $100.00 Call",
        action="Purchase", position_type="Margin",
        values=("2", "3", "600", "$600"),
    )))

    assert trades[0].action == "Buy to Open"
    # 期权乘数是 100，勾稽通过说明它被正确应用。
    assert trades[0].amount == "-600"
    assert trades[0].description == "CALL INTEL CORP $100.00 EXP 03/19/27"


@pytest.mark.parametrize(
    ("action", "position_type", "expected"),
    [
        ("Purchase", "Margin", "Buy to Open"),
        ("Sale", "Margin", "Sell to Close"),
        ("Sale", "Short", "Sell to Open"),
        ("Purchase", "Short", "Buy to Close"),
    ],
)
def test_all_four_option_actions(action: str, position_type: str, expected: str) -> None:
    """四个组合各自映射到正确的开平仓动作。"""
    trades = parse_econfirm_body("m1", _body(_block(
        symbol="INTC 03/19/2027 100.00 C", description="INTEL CORP 03/19/2027 $100.00 Call",
        action=action, position_type=position_type,
        values=("1", "3", "300", "$300"),
    )))

    assert trades[0].action == expected


def test_unknown_option_action_is_rejected() -> None:
    """无法映射的组合必须报错并给出实际值。"""
    with pytest.raises(IngestError) as raised:
        parse_econfirm_body("m1", _body(_block(
            symbol="INTC 03/19/2027 100.00 C", action="Purchase", position_type="Weird",
            values=("1", "3", "300", "$300"),
        )))

    assert "无法映射 Action/Type" in str(raised.value)
    assert "Weird" in str(raised.value)


def test_option_multiplier_is_applied() -> None:
    """期权一笔的本金 = 数量 × 价格 × 100。"""
    trades = parse_econfirm_body("m1", _body(_block(
        symbol="INTC 03/19/2027 100.00 C", description="X 03/19/2027 $100.00 Call",
        values=("1", "3", "300", "$300"),
    )))

    # 若乘数没被应用，本金 300 会被判为不符（期望 3）。
    assert trades[0].amount == "-300"


# --------------------------------------------------------------------------
# 结构
# --------------------------------------------------------------------------


def test_body_without_a_symbol_block_is_rejected() -> None:
    """没有 ``Symbol:`` 块即报错。"""
    with pytest.raises(IngestError, match="正文中没有交易块"):
        parse_econfirm_body("m1", "just some text without symbol blocks")


def test_multiple_symbol_blocks_yield_multiple_trades() -> None:
    """**每块对应一个证券**，多块产出多笔。"""
    trades = parse_econfirm_body("m1", _body(
        _block(symbol="INTC"),
        _block(symbol="AMD", description="ADVANCED MICRO"),
    ))

    assert [trade.symbol for trade in trades] == ["INTC", "AMD"]


def test_one_block_can_yield_multiple_trades() -> None:
    """**一个块内的金额表可以含多笔成交。**"""
    trades = parse_econfirm_body("m1", _body(_block(values=(
        "2", "40", "80", "n/a:", "$0.00", "n/a", "$80",
        "for the above:",
        "3", "40", "120", "n/a:", "$0.00", "n/a", "$120",
        "Totals", "5", "x", "y", "$0.00", "$200",
    ))))

    assert [trade.quantity for trade in trades] == ["2", "3"]


def test_html_charge_interest_column_parses_every_zero_charge_fill() -> None:
    """五列 eConfirm 模板按每个 N/A fill 解析，且逐笔与 Totals 勾稽。"""
    normalized = html_to_text(_MULTI_PRICE_HTML)
    trades = parse_econfirm_body("synthetic", normalized)

    assert [(trade.quantity, trade.price, trade.fees, trade.amount) for trade in trades] == [
        ("2", "10.00", "0", "-20.00"),
        ("3", "11.00", "0", "-33.00"),
        ("1", "12.00", "0", "-12.00"),
    ]


def test_html_charge_interest_parses_explicit_fees_per_fill() -> None:
    """五列行可含 Commission、Industry Fee 或 N/A，且逐腿值与 Totals 勾稽。"""
    trades = parse_econfirm_body("synthetic", html_to_text(_LABELED_FEES_HTML))

    assert [(trade.quantity, trade.price, trade.fees, trade.amount) for trade in trades] == [
        ("2", "10.00", "0.05", "-20.05"),
        ("3", "11.00", "0.02", "-33.02"),
        ("1", "12.00", "0", "-12.00"),
    ]
    assert sum((Decimal(trade.fees) for trade in trades), Decimal(0)) == Decimal("0.07")
    assert sum((abs(Decimal(trade.amount)) for trade in trades), Decimal(0)) == Decimal("65.07")


def test_charge_interest_rows_keep_leg_separators_and_safe_notes() -> None:
    """五列多腿数据可保留分隔符；只跳过分隔符后无金额的普通说明。"""
    trades = parse_value_rows(
        values=(
            "2", "$10.00", "$20.00", "N/A", "$20.00",
            "for the above:",
            "- Industry Fee: For certain securities transactions, synthetic example",
            "3", "$11.00", "$33.00", "Industry Fee:", "$0.02", "$33.02",
            "for the above:", "- The unit price shown is an average price.",
            "1", "$12.00", "$12.00", "N/A", "$12.00",
            "for the above:", "- Schwab acted as your agent.",
            "1", "$13.00", "$13.00", "N/A", "$13.00",
            "Totals", "7", "x", "x", "$0.02", "$78.02",
        ),
        where="synthetic",
        trade_date="2026-04-05",
        action="Buy",
        symbol="XYZ",
        description="SYNTHETIC CORP",
        multiplier=1,
        has_charge_interest_column=True,
    )

    assert len(trades) == 4
    assert [(trade.quantity, trade.price, trade.fees, trade.amount) for trade in trades] == [
        ("2", "10.00", "0", "-20.00"),
        ("3", "11.00", "0.02", "-33.02"),
        ("1", "12.00", "0", "-12.00"),
        ("1", "13.00", "0", "-13.00"),
    ]


@pytest.mark.parametrize(
    "note",
    [
        "Synthetic execution note",
        "- Industry Fee: For certain securities transactions in 2026",
        "- Industry Fee: For certain securities transactions costing $1.00",
        "- Schwab acted as your agent. Additional unapproved text",
        "- Schwab is acting as your agent.",
    ],
)
def test_charge_interest_separator_rejects_unknown_or_numeric_notes(note: str) -> None:
    """只接受已知脚注前缀，未知文本和含数字/金额的脚注都必须失败。"""
    with pytest.raises(IngestError, match="分隔符后含未识别的字段"):
        parse_value_rows(
            values=(
                "2", "$10.00", "$20.00", "N/A", "$20.00",
                "for the above:", note,
                "Totals", "2", "x", "x", "$0.00", "$20.00",
            ),
            where="synthetic",
            trade_date="2026-04-05",
            action="Buy",
            symbol="XYZ",
            description="SYNTHETIC CORP",
            multiplier=1,
            has_charge_interest_column=True,
        )


def test_parse_econfirm_uses_complete_html_candidate_when_plain_is_incomplete() -> None:
    """纯文本候选缺少字段时，完整 HTML 候选仍须保留所有逐笔成交。"""
    message = GmailMessage(
        message_id="synthetic",
        sender="donotreply@mail.schwab.com",
        subject="Schwab eConfirms",
        body="Symbol:\n\nXYZ\n\nSecurity Description:\n\nSYNTHETIC CORP",
        email_timestamp=None,
        body_candidates=(html_to_text(_MULTI_PRICE_HTML),),
    )

    trades = parse_econfirm(message)

    assert len(trades) == 3
    assert sum((Decimal(trade.quantity) for trade in trades), Decimal(0)) == Decimal("6")
    assert sum((abs(Decimal(trade.amount)) for trade in trades), Decimal(0)) == Decimal("65")


def test_charge_interest_amount_is_not_reclassified_as_a_fee() -> None:
    """非 N/A 的 Charge and/or Interest 不能被猜成佣金或手续费。"""
    html = _MULTI_PRICE_HTML.replace("<td>N/A</td>", "<td>$1.00</td>", 1)

    with pytest.raises(IngestError, match="只接受 N/A 或明确标注"):
        parse_econfirm_body("synthetic", html_to_text(html))


def test_unknown_charge_interest_label_is_not_reclassified() -> None:
    """费用列里的未知标签不能被忽略或按金额猜分类。"""
    html = _LABELED_FEES_HTML.replace(
        "Industry Fee:<br>$0.02", "Processing Fee:<br>$0.02", 1,
    )

    with pytest.raises(IngestError, match="只接受 N/A 或明确标注"):
        parse_econfirm_body("synthetic", html_to_text(html))


def test_charge_interest_layout_rejects_a_mismatched_total() -> None:
    """五列模板的 Totals 金额必须与全部逐腿净额精确勾稽。"""
    prefix, _ = _MULTI_PRICE_HTML.rsplit("<td>$65.00</td>", 1)
    html = f"{prefix}<td>$66.00</td>"

    with pytest.raises(IngestError, match="金额与 Totals 不一致"):
        parse_econfirm_body("synthetic", html_to_text(html))


def test_multi_leg_rows_use_the_labeled_fee_parser() -> None:
    """**有分隔符的腿会用带标签的费用表解析**（与参考实现一致）。

    因此腿尾需要完整的费用结构；只给一个裸金额会报"缺少 Industry Fee 标签"。
    这与"紧凑尾部"那三种形态不同——那些只用于**单笔**路径。
    """
    with pytest.raises(IngestError, match="缺少 Industry Fee 标签"):
        parse_econfirm_body("m1", _body(_block(values=(
            "2", "40", "80", "$80",
            "for the above:",
            "3", "40", "120", "$120",
            "Totals", "5", "x", "y", "$0.00", "$200",
        ))))


def test_additional_information_is_excluded() -> None:
    """**附加说明之后的内容不属于金额表。**

    留着它会让后面的行被当成金额表的一部分，从而报出"无法识别结构"或算出错误金额。
    """
    extra = f"\n\n{ADDITIONAL_INFORMATION_MARKER}\n\nSome unrelated text\n\n$999"
    trades = parse_econfirm_body("m1", _body(_block(extra=extra)))

    assert len(trades) == 1
    assert trades[0].amount == "-405"


def test_missing_total_amount_marker_is_rejected() -> None:
    """缺少 ``Total Amount`` 标记即报错。"""
    block = _block().replace("Total Amount", "Something Else")

    with pytest.raises(IngestError, match="缺少成交金额表"):
        parse_econfirm_body("m1", _body(block))


@pytest.mark.parametrize("label", ["Security Description", "Action", "Type", "Trade Date"])
def test_missing_required_fields_are_rejected(label: str) -> None:
    """任一必填字段缺失即报错。"""
    block = _block()
    # 把该字段的标签改掉，字段随之缺失。
    block = block.replace(f"{label}:\n", f"Not{label}:\n", 1)

    with pytest.raises(IngestError, match=f"缺少字段 '{label}'"):
        parse_econfirm_body("m1", _body(block))


def test_errors_identify_which_trade() -> None:
    """**错误带"第几笔"**，便于定位到具体块。"""
    with pytest.raises(IngestError) as raised:
        parse_econfirm_body("m1", _body(
            _block(),
            _block(symbol="AMD").replace("Action:\n\nPurchase", "Action:\n\nBogus"),
        ))

    assert "第2笔" in str(raised.value)


# --------------------------------------------------------------------------
# 入口：候选一致性
# --------------------------------------------------------------------------


def test_parse_econfirm_accepts_two_agreeing_candidates() -> None:
    """两个候选解析出同一批交易时正常返回。"""
    body = _body(_block())
    message = GmailMessage(
        message_id="m1", sender="s", subject="Schwab eConfirms",
        body=body, email_timestamp=None, body_candidates=(body,),
    )

    trades = parse_econfirm(message)

    assert len(trades) == 1
    assert trades[0].symbol == "INTC"


def test_parse_econfirm_rejects_disagreeing_candidates() -> None:
    """**两个候选结果不同时报错，不挑一个用。**"""
    message = GmailMessage(
        message_id="m1", sender="s", subject="Schwab eConfirms",
        body=_body(_block(symbol="INTC")),
        email_timestamp=None,
        body_candidates=(_body(_block(symbol="AMD", description="ADVANCED MICRO")),),
    )

    with pytest.raises(IngestError, match="解析出的交易不一致"):
        parse_econfirm(message)


def test_parse_econfirm_reports_all_candidate_failures() -> None:
    """全部候选都失败时报错并汇总各自原因。"""
    message = GmailMessage(
        message_id="m1", sender="s", subject="Schwab eConfirms",
        body="no blocks", email_timestamp=None, body_candidates=("also no blocks",),
    )

    with pytest.raises(IngestError) as raised:
        parse_econfirm(message)

    assert "所有正文候选解析失败" in str(raised.value)
    assert "候选1" in str(raised.value) and "候选2" in str(raised.value)


def test_parse_econfirm_succeeds_when_only_one_candidate_parses() -> None:
    """只有一个候选解析得出来时正常返回——另一方失败不致命。"""
    message = GmailMessage(
        message_id="m1", sender="s", subject="Schwab eConfirms",
        body=_body(_block()), email_timestamp=None,
        body_candidates=("garbage without blocks",),
    )

    assert len(parse_econfirm(message)) == 1
