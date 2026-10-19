"""期权 Sale/Cash 只能通过精确既有成交事实补全开平仓动作。"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from html import escape

import pytest

from lyra.domain.gmail.econfirm import (
    GmailMessage,
    UnresolvedCashOptionSaleError,
    email_trade_to_raw_fields,
    parse_econfirm,
    parse_econfirm_body,
    trades_fingerprint,
)
from lyra.domain.ledger.errors import IngestError
from lyra.domain.ledger.transactions import ParseTransactionContext, parse_transaction_row
from lyra.domain.ledger.types import ParsedTransaction, RawTransactionFields
from lyra.infrastructure.gmail.html_text import html_to_text


_SYMBOL = "ACME 03/19/2027 100.00 C"
_SECURITY_DESCRIPTION = "ACME CORP 03/19/2027 $100.00 Call"
_NORMALIZED_DESCRIPTION = "CALL ACME CORP $100.00 EXP 03/19/27"
_TRADE_DATE = "2026-01-05"


def _cash_sale_body(
    *,
    action: str = "Sale",
    position_type: str = "Cash",
    trade_date: str = "1/5/2026",
    symbol: str = _SYMBOL,
    quantity: str = "1",
    price: str = "3.00",
    principal: str = "300.00",
    amount: str = "$300.00",
) -> str:
    """构造只含合成证券和金额的最小 eConfirm 正文。"""
    return (
        "Schwab eConfirms\n\nAccount ending in 276\n\n"
        f"Symbol:\n\n{symbol}\n\n"
        f"Security Description:\n\n{_SECURITY_DESCRIPTION}\n\n"
        f"Action:\n\n{action}\n\n"
        f"Type:\n\n{position_type}\n\n"
        f"Trade Date:\n\n{trade_date}\n\n"
        "Total Amount\n\n"
        f"{quantity}\n\n{price}\n\n{principal}\n\n{amount}"
    )


def _combine_blocks(*bodies: str) -> str:
    """组合多个合成交易块，模拟同一封通知中的多笔证券成交。"""
    header = "Schwab eConfirms\n\nAccount ending in 276\n\n"
    blocks = ["Symbol:\n" + body.split("Symbol:\n", 1)[1] for body in bodies]
    return header + "\n\n".join(blocks)


def _existing_option_sale(
    *,
    action: str = "Sell to Open",
    account: str = "synthetic-account-276",
    record_date: str = "2026-01-05",
    trade_date: str = _TRADE_DATE,
    symbol: str = _SYMBOL,
    description: str = _NORMALIZED_DESCRIPTION,
    quantity: str = "1",
    price: str = "$3.00",
    fees: str = "",
    amount: str = "$300.00",
) -> ParsedTransaction:
    """通过生产账本行解析器创建合成既有期权卖出。"""
    date_text = (
        _us_date(record_date) if record_date == trade_date
        else f"{_us_date(record_date)} as of {_us_date(trade_date)}"
    )
    return parse_transaction_row(
        RawTransactionFields(
            date=date_text,
            action=action,
            symbol=symbol,
            description=description,
            quantity=quantity,
            price=price,
            fees=fees,
            amount=amount,
        ),
        ParseTransactionContext(
            account=account,
            source_hash="synthetic-source-hash",
            source_type="csv",
            source_reference="synthetic.csv",
            sequence=1,
            where="synthetic fixture",
        ),
    )


def _us_date(value: str) -> str:
    """把合成 ISO 日期转为解析器使用的美式日期。"""
    year, month, day = value.split("-")
    return f"{int(month)}/{int(day)}/{year}"


def _as_html(body: str) -> str:
    """把合成纯文本的每个非空行转为有块边界的 HTML。"""
    lines = (line for line in body.splitlines() if line.strip())
    return "<html><body>" + "".join(f"<div>{escape(line)}</div>" for line in lines) + "</body></html>"


def _parse_with_context(
    body: str,
    existing: tuple[ParsedTransaction, ...],
):
    """使用指定的既有成交上下文解析合成邮件。"""
    return parse_econfirm_body(
        "synthetic-cash-sale",
        body,
        existing_option_sales=existing,
    )


def test_cash_option_sale_without_context_has_a_specific_error() -> None:
    """无上下文时显式失败，不能仅凭 Sale/Cash 猜 STO 或 STC。"""
    with pytest.raises(UnresolvedCashOptionSaleError, match="无法确定开仓或平仓"):
        parse_econfirm_body("synthetic", _cash_sale_body())


def test_cash_option_sale_reuses_unique_existing_action_and_is_marked_existing_only() -> None:
    """匹配精确既有成交后回填动作，并保留 no-insert 控制标记。"""
    trade = _parse_with_context(
        _cash_sale_body(), (_existing_option_sale(action="Sell to Close"),)
    )[0]

    assert trade.action == "Sell to Close"
    assert trade.existing_only is True
    assert trade.action != "Sell"
    raw_fields = email_trade_to_raw_fields(trade)
    assert raw_fields.action == "Sell to Close"
    assert not hasattr(raw_fields, "existing_only")


def test_existing_description_and_record_date_rebuild_the_unique_business_hash() -> None:
    """既有 CSV 描述可与邮件不同，记录日也可异于交易日，仍按唯一哈希精确关联。"""
    existing = _existing_option_sale(
        record_date="2026-01-06",
        description="SYNTHETIC LEGACY OPTION DESCRIPTION",
    )

    trade = _parse_with_context(_cash_sale_body(), (existing,))[0]

    assert trade.action == "Sell to Open"
    assert trade.existing_only is True
    # 关联已有事实仅用于动作和 no-insert 控制；邮件原描述仍原样留在解析结果。
    assert trade.description == _NORMALIZED_DESCRIPTION


def test_cash_option_sale_without_a_matching_existing_fact_is_unresolved() -> None:
    """价款不同的既有行不能仅因合约相同而补动作。"""
    existing = _existing_option_sale(price="$4.00", amount="$400.00")
    with pytest.raises(UnresolvedCashOptionSaleError, match="无法唯一关联"):
        _parse_with_context(_cash_sale_body(), (existing,))


@pytest.mark.parametrize(
    "existing",
    [
        _existing_option_sale(trade_date="2026-01-06"),
        _existing_option_sale(symbol="ACME 03/19/2027 101.00 C"),
        replace(_existing_option_sale(), underlying="OTHER"),
        replace(_existing_option_sale(), expiry="2027-03-26"),
        replace(_existing_option_sale(), strike=Decimal("101.00")),
        replace(_existing_option_sale(), option_type="P"),
        _existing_option_sale(price="$3.0"),
        _existing_option_sale(amount="$300.0"),
        _existing_option_sale(
            action="Sell to Open", fees="$0.01", amount="$299.99"
        ),
    ],
    ids=(
        "trade-date", "raw-symbol", "underlying", "expiry", "strike", "put-call",
        "price-precision", "amount-precision", "numeric-value",
    ),
)
def test_cash_option_sale_requires_every_identity_and_numeric_field_to_match(
    existing: ParsedTransaction,
) -> None:
    """日期、完整合约身份、费用与十进制精度均是匹配条件。"""
    with pytest.raises(UnresolvedCashOptionSaleError):
        _parse_with_context(_cash_sale_body(), (existing,))


def test_cash_option_sale_rejects_a_snapshot_mixed_across_accounts() -> None:
    """即使一行看似匹配，也不能从混合账户快照中跨账户补动作。"""
    first = _existing_option_sale()
    second = replace(_existing_option_sale(action="Sell to Close"), account="other-account")
    with pytest.raises(UnresolvedCashOptionSaleError, match="唯一账户"):
        _parse_with_context(_cash_sale_body(), (first, second))


@pytest.mark.parametrize(
    "existing",
    [
        (
            _existing_option_sale(action="Sell to Open"),
            _existing_option_sale(action="Sell to Close"),
        ),
        (
            _existing_option_sale(record_date="2026-01-05"),
            _existing_option_sale(record_date="2026-01-06"),
        ),
        (
            _existing_option_sale(description="LEGACY DESCRIPTION A"),
            _existing_option_sale(description="LEGACY DESCRIPTION B"),
        ),
    ],
    ids=("open-vs-close", "multiple-record-date-hashes", "multiple-description-hashes"),
)
def test_cash_option_sale_rejects_multiple_matching_business_hashes(
    existing: tuple[ParsedTransaction, ...],
) -> None:
    """多个动作或多个业务哈希都不能被挑选。"""
    with pytest.raises(UnresolvedCashOptionSaleError, match="无法唯一关联"):
        _parse_with_context(_cash_sale_body(), existing)


def test_plain_and_html_candidates_resolve_the_same_existing_only_trade() -> None:
    """纯文本与 HTML 同时关联到同一事实时保留一致的 existing-only 标记。"""
    body = _cash_sale_body()
    message = GmailMessage(
        message_id="synthetic",
        sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms",
        body=body,
        email_timestamp=None,
        body_candidates=(html_to_text(_as_html(body)),),
    )

    trades = parse_econfirm(
        message,
        existing_option_sales=(_existing_option_sale(),),
    )
    assert len(trades) == 1
    assert trades[0].action == "Sell to Open"
    assert trades[0].existing_only is True
    assert trades_fingerprint(trades) == trades_fingerprint(
        parse_econfirm_body(
            "synthetic",
            html_to_text(_as_html(body)),
            existing_option_sales=(_existing_option_sale(),),
        )
    )


def test_cash_mime_bridge_normalizes_only_trailing_zero_quantity_and_price() -> None:
    """唯一严格命中的 HTML Cash 行可规范化同封 plain 的尾随零显示差异。"""
    canonical_body = _cash_sale_body()
    display_variant = _cash_sale_body(
        quantity="1.00000",
        price="3.000000000",
        principal="300.00",
        amount="$300.00",
    )
    message = GmailMessage(
        message_id="synthetic-bridge",
        sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms",
        body=display_variant,
        email_timestamp=None,
        body_candidates=(html_to_text(_as_html(canonical_body)),),
    )

    trades = parse_econfirm(
        message,
        existing_option_sales=(_existing_option_sale(),),
    )

    assert trades[0].quantity == "1"
    assert trades[0].price == "3.00"
    assert trades[0].existing_only is True
    assert trades[0].action == "Sell to Open"


def test_cash_mime_bridge_rejects_a_real_numeric_difference() -> None:
    """桥接不能把价格变化或其对应的本金/净额变化当作展示精度差异。"""
    canonical_body = _cash_sale_body()
    changed_body = _cash_sale_body(
        quantity="1.00000",
        price="3.01",
        principal="301.00",
        amount="$301.00",
    )
    message = GmailMessage(
        message_id="synthetic-bridge-mismatch",
        sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms",
        body=changed_body,
        email_timestamp=None,
        body_candidates=(html_to_text(_as_html(canonical_body)),),
    )

    with pytest.raises(UnresolvedCashOptionSaleError, match="不一致"):
        parse_econfirm(
            message,
            existing_option_sales=(_existing_option_sale(),),
        )


def test_cash_mime_bridge_rejects_a_different_principal_within_reconciliation_tolerance() -> None:
    """Cash 桥接不复用建账容差吞掉本金字段的真实差异。"""
    canonical_body = _cash_sale_body()
    changed_body = _cash_sale_body(
        quantity="1.00000",
        price="3.000000000",
        principal="300.01",
        amount="$300.00",
    )
    message = GmailMessage(
        message_id="synthetic-principal-mismatch",
        sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms",
        body=changed_body,
        email_timestamp=None,
        body_candidates=(html_to_text(_as_html(canonical_body)),),
    )

    with pytest.raises(UnresolvedCashOptionSaleError, match="不一致"):
        parse_econfirm(
            message,
            existing_option_sales=(_existing_option_sale(),),
        )


def test_cash_mime_bridge_cannot_be_reused_for_an_unmatched_new_cash_row() -> None:
    """同一正文中已验证的一行不能为另一条新 Cash 成交背书。"""
    canonical_body = _cash_sale_body()
    matching_variant = _cash_sale_body(
        quantity="1.00000",
        price="3.000000000",
        principal="300.00",
        amount="$300.00",
    )
    unmatched = _cash_sale_body(
        symbol="ACME 03/19/2027 105.00 C",
        quantity="1",
        price="2.00",
        principal="200.00",
        amount="$200.00",
    )
    message = GmailMessage(
        message_id="synthetic-new-cash",
        sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms",
        body=_combine_blocks(matching_variant, unmatched),
        email_timestamp=None,
        body_candidates=(html_to_text(_as_html(canonical_body)),),
    )

    with pytest.raises(UnresolvedCashOptionSaleError):
        parse_econfirm(
            message,
            existing_option_sales=(_existing_option_sale(),),
        )


def test_cash_mime_bridge_scans_cash_after_an_earlier_malformed_block() -> None:
    """普通块提前报错不能遮住其后的未匹配 Cash 成交。"""
    canonical_body = _cash_sale_body()
    matching_variant = _cash_sale_body(
        quantity="1.00000",
        price="3.000000000",
        principal="300.00",
        amount="$300.00",
    )
    malformed = _cash_sale_body(action="Unknown", position_type="Short")
    unmatched = _cash_sale_body(
        symbol="ACME 03/19/2027 105.00 C",
        quantity="1",
        price="2.00",
        principal="200.00",
        amount="$200.00",
    )
    message = GmailMessage(
        message_id="synthetic-hidden-new-cash",
        sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms",
        body=_combine_blocks(matching_variant, malformed, unmatched),
        email_timestamp=None,
        body_candidates=(html_to_text(_as_html(canonical_body)),),
    )

    with pytest.raises(UnresolvedCashOptionSaleError):
        parse_econfirm(
            message,
            existing_option_sales=(_existing_option_sale(),),
        )


def test_verified_cash_can_still_fallback_when_a_later_ordinary_block_is_malformed() -> None:
    """全部 Cash 行先独立验证后，普通块格式错误仍可沿用唯一严格候选。"""
    canonical_body = _cash_sale_body()
    matching_variant = _cash_sale_body(
        quantity="1.00000",
        price="3.000000000",
        principal="300.00",
        amount="$300.00",
    )
    malformed = _cash_sale_body(action="Unknown", position_type="Short")
    message = GmailMessage(
        message_id="synthetic-verified-cash-malformed-block",
        sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms",
        body=_combine_blocks(matching_variant, malformed),
        email_timestamp=None,
        body_candidates=(html_to_text(_as_html(canonical_body)),),
    )

    trades = parse_econfirm(
        message,
        existing_option_sales=(_existing_option_sale(),),
    )

    assert len(trades) == 1
    assert trades[0].existing_only is True
    assert trades[0].action == "Sell to Open"


def test_cash_mime_bridge_does_not_hide_changes_to_other_known_rows() -> None:
    """Cash 行规范化后仍要比较整封邮件中的其他已知动作和数值。"""
    canonical_cash = _cash_sale_body()
    candidate_cash = _cash_sale_body(
        quantity="1.00000",
        price="3.000000000",
        principal="300.00",
        amount="$300.00",
    )
    canonical_short = _cash_sale_body(
        action="Sale",
        position_type="Short",
        symbol="BETA 03/19/2027 50.00 C",
        price="2.00",
        principal="200.00",
        amount="$200.00",
    )
    changed_short = _cash_sale_body(
        action="Sale",
        position_type="Short",
        symbol="BETA 03/19/2027 50.00 C",
        price="2.01",
        principal="201.00",
        amount="$201.00",
    )
    message = GmailMessage(
        message_id="synthetic-known-row-mismatch",
        sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms",
        body=_combine_blocks(candidate_cash, changed_short),
        email_timestamp=None,
        body_candidates=(html_to_text(_as_html(_combine_blocks(canonical_cash, canonical_short))),),
    )

    with pytest.raises(IngestError, match="解析出的交易不一致"):
        parse_econfirm(
            message,
            existing_option_sales=(_existing_option_sale(),),
        )


def test_cash_candidate_cannot_be_masked_by_a_known_short_candidate() -> None:
    """一份正文的 Sale/Cash 不明确时，另一份已知动作不能替它背书。"""
    cash_body = _cash_sale_body()
    known_body = _cash_sale_body(action="Sale", position_type="Short")
    message = GmailMessage(
        message_id="synthetic",
        sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms",
        body=known_body,
        email_timestamp=None,
        body_candidates=(html_to_text(_as_html(cash_body)),),
    )

    with pytest.raises(UnresolvedCashOptionSaleError, match="候选无法一致验证"):
        parse_econfirm(message)


def test_existing_only_marker_participates_in_candidate_fingerprint() -> None:
    """经济字段相同但入账语义不同的候选必须得到不同指纹。"""
    body = _cash_sale_body()
    matched = _parse_with_context(body, (_existing_option_sale(),))[0]
    known = parse_econfirm_body(
        "synthetic",
        _cash_sale_body(action="Sale", position_type="Short"),
    )[0]

    assert matched.action == known.action == "Sell to Open"
    assert matched.existing_only is True and known.existing_only is False
    assert trades_fingerprint((matched,)) != trades_fingerprint((known,))


@pytest.mark.parametrize("cash_is_plain", (True, False), ids=("cash-first", "cash-last"))
def test_known_short_and_resolved_cash_candidates_still_conflict(
    cash_is_plain: bool,
) -> None:
    """动作相同但 only-existing 标记不同的 MIME 候选不能按遍历顺序覆盖。"""
    cash_body = _cash_sale_body()
    known_body = _cash_sale_body(action="Sale", position_type="Short")
    message = GmailMessage(
        message_id="synthetic",
        sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms",
        body=cash_body if cash_is_plain else known_body,
        email_timestamp=None,
        body_candidates=(
            html_to_text(_as_html(known_body if cash_is_plain else cash_body)),
        ),
    )

    with pytest.raises(IngestError, match="解析出的交易不一致"):
        parse_econfirm(
            message,
            existing_option_sales=(_existing_option_sale(action="Sell to Open"),),
        )


def test_plain_and_html_cash_candidates_preserve_the_specific_unresolved_error() -> None:
    """所有正文候选均无法匹配时，入口聚合保留专用异常类型。"""
    body = _cash_sale_body()
    message = GmailMessage(
        message_id="synthetic",
        sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms",
        body=body,
        email_timestamp=None,
        body_candidates=(html_to_text(_as_html(body)),),
    )

    with pytest.raises(UnresolvedCashOptionSaleError):
        parse_econfirm(message)


def test_unrelated_candidate_parse_errors_remain_visible_with_cash_sale_error() -> None:
    """保留 Sale/Cash 专用错误时，也把其他正文结构错误带给调用方。"""
    message = GmailMessage(
        message_id="synthetic",
        sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms",
        body=_cash_sale_body(),
        email_timestamp=None,
        body_candidates=("malformed candidate",),
    )

    with pytest.raises(
        UnresolvedCashOptionSaleError,
        match="候选2: 正文中没有交易块",
    ) as raised:
        parse_econfirm(message)
    assert isinstance(raised.value, IngestError)

