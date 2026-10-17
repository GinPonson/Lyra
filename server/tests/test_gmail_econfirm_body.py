"""`parse_econfirm_body` 端到端测试。

正文被 ``Symbol:`` 分成若干块（第一段是前言），**每块对应一个证券**；一个块内的金额表
又可以含多笔成交（多价格成交），因此返回值是全部成交的拼接。
"""

from __future__ import annotations

import pytest

from lyra.domain.gmail.econfirm import (
    ADDITIONAL_INFORMATION_MARKER,
    GmailMessage,
    parse_econfirm,
    parse_econfirm_body,
)
from lyra.domain.ledger.errors import IngestError


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
