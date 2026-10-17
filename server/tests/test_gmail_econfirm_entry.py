"""eConfirm 解析入口层测试。

两处"宁可失败也不猜"的设计：

1. **账户尾号必须唯一**——主题与正文说法矛盾时不能挑一个用；
2. **纯文本与 HTML 必须解析出同一批交易**——结果不同说明至少一方错了。
"""

from __future__ import annotations

import pytest

from lyra.domain.gmail.econfirm import (
    ACCOUNT_SUFFIX_PATTERN,
    OPTION_ACTIONS,
    STOCK_ACTIONS,
    GmailMessage,
    candidate_bodies,
    extract_account_suffix,
    trades_fingerprint,
)
from lyra.domain.ledger.errors import IngestError


def _message(*, subject="Schwab eConfirms", body="", candidates=()) -> GmailMessage:
    """构造邮件。"""
    return GmailMessage(
        message_id="m1", sender="Schwab <donotreply@mail.schwab.com>",
        subject=subject, body=body, email_timestamp=None,
        body_candidates=tuple(candidates),
    )


# --------------------------------------------------------------------------
# 动作表
# --------------------------------------------------------------------------


def test_option_actions_combine_side_and_position_type() -> None:
    """**期权动作由买卖方向与保证金/做空共同决定。**

    只看买卖会丢掉开平仓的区别——把"买入开仓"记成"买入平仓"会让持仓数量算错。
    """
    assert OPTION_ACTIONS == {
        "Purchase|Margin": "Buy to Open",
        "Sale|Margin": "Sell to Close",
        "Sale|Short": "Sell to Open",
        "Purchase|Short": "Buy to Close",
    }


def test_stock_actions_ignore_position_type() -> None:
    """**股票不区分开平仓**，只由买卖方向决定。"""
    assert STOCK_ACTIONS == {"Purchase": "Buy", "Sale": "Sell"}


# --------------------------------------------------------------------------
# 正文候选
# --------------------------------------------------------------------------


def test_candidate_bodies_prefer_plain_then_html() -> None:
    """先纯文本、后 HTML。"""
    message = _message(body="plain", candidates=("html",))
    assert candidate_bodies(message) == ("plain", "html")


def test_candidate_bodies_drop_blanks_and_duplicates() -> None:
    """空候选与重复候选被剔除。"""
    message = _message(body="plain", candidates=("  ", "plain", "html"))
    assert candidate_bodies(message) == ("plain", "html")


def test_candidate_bodies_order_is_preserved() -> None:
    """顺序稳定。"""
    message = _message(body="a", candidates=("b", "c"))
    assert candidate_bodies(message) == ("a", "b", "c")


# --------------------------------------------------------------------------
# 账户尾号
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Account ending in 276",
        "Account ending: 276",
        "account ending in ABC1",
    ],
)
def test_extracts_the_suffix(text: str) -> None:
    """两种写法都能提取。"""
    assert extract_account_suffix(_message(body=text)) == text.rsplit(" ", 1)[-1].rstrip(":")


def test_suffix_from_the_subject() -> None:
    """主题里的尾号也认。"""
    assert extract_account_suffix(
        _message(subject="Schwab eConfirms Account ending in 276", body="no suffix here")
    ) == "276"


def test_conflicting_suffixes_are_rejected() -> None:
    """**主题与正文尾号冲突时报错，不挑一个用。**

    猜一个会把交易写进错误的账本，而更正它意味着删除已入账的交易并重算 FIFO。
    """
    with pytest.raises(IngestError) as raised:
        extract_account_suffix(_message(
            subject="Account ending in 276", body="Account ending in 999"
        ))

    assert "账户尾号不一致" in str(raised.value)
    # 报错里列出两个候选，便于排查。
    assert "276" in str(raised.value) and "999" in str(raised.value)


def test_missing_suffix_is_rejected() -> None:
    """没有尾号时报错。"""
    with pytest.raises(IngestError, match="缺少 Account ending"):
        extract_account_suffix(_message(body="no suffix"))


def test_duplicate_identical_suffixes_are_fine() -> None:
    """**同一尾号出现多次是正常的**（主题与正文各一次）。"""
    assert extract_account_suffix(_message(
        subject="Account ending in 276", body="Account ending in 276"
    )) == "276"


def test_suffix_pattern_requires_the_account_ending_prefix() -> None:
    """光有数字不构成尾号。"""
    assert ACCOUNT_SUFFIX_PATTERN.search("ending 276") is None
    assert ACCOUNT_SUFFIX_PATTERN.search("Account ending in 276") is not None


# --------------------------------------------------------------------------
# 指纹
# --------------------------------------------------------------------------


def test_fingerprint_is_stable_for_equal_trades() -> None:
    """相同成交得到相同指纹。"""
    from lyra.domain.gmail.econfirm import EmailTrade

    def trade(quantity: str) -> EmailTrade:
        return EmailTrade(
            trade_date="2026-01-05", action="Buy", symbol="INTC",
            description="INTEL CORP", quantity=quantity, price="40.5",
            fees="0", amount="-405",
        )

    assert trades_fingerprint((trade("10"),)) == trades_fingerprint((trade("10"),))


def test_fingerprint_differs_when_any_field_differs() -> None:
    """任一字段不同则指纹不同。"""
    from lyra.domain.gmail.econfirm import EmailTrade

    def trade(**overrides) -> EmailTrade:
        base = {
            "trade_date": "2026-01-05", "action": "Buy", "symbol": "INTC",
            "description": "INTEL CORP", "quantity": "10", "price": "40.5",
            "fees": "0", "amount": "-405",
        }
        base.update(overrides)
        return EmailTrade(**base)  # type: ignore[arg-type]

    assert trades_fingerprint((trade(),)) != trades_fingerprint((trade(quantity="11"),))
    assert trades_fingerprint((trade(),)) != trades_fingerprint((trade(symbol="AMD"),))
    assert trades_fingerprint((trade(),)) != trades_fingerprint((trade(action="Sell"),))
