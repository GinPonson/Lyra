"""覆盖同一标的同时存在多空批次时的方向隔离与费用分摊。"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from lyra.domain.fifo.replay import replay_fifo
from lyra.domain.fifo.types import FifoError
from lyra.domain.ledger.types import ParsedTransaction


def _transaction(
    index: int,
    action: str,
    transaction_date: str,
    *,
    quantity: str,
    price: str,
    fees: str,
) -> ParsedTransaction:
    """构造一笔同账户、同股票、但拥有稳定唯一哈希的标准交易。"""
    return ParsedTransaction(
        account="TST001",
        source_type="csv",
        source_reference="fixture.csv",
        record_date=transaction_date,
        transaction_date=transaction_date,
        action=action,  # type: ignore[arg-type]
        raw_symbol="ACME",
        description="ACME",
        asset_type="stock",
        underlying="ACME",
        expiry=None,
        strike=None,
        option_type=None,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fees=Decimal(fees),
        amount=None,
        source_hash="s" * 64,
        sequence=index,
        occurrence=1,
        business_hash=hashlib.sha256(f"business-{index}".encode()).hexdigest(),
        transaction_hash=hashlib.sha256(f"transaction-{index}".encode()).hexdigest(),
    )


def test_close_actions_consume_only_their_direction_and_allocate_fees() -> None:
    """混合多空批次按方向各自 FIFO，并按数量分配开平仓费用。"""
    transactions = (
        # 空头先进入账本队列，后面的平多不能误消耗它。
        _transaction(1, "Sell to Open", "2026-01-05", quantity="3", price="20", fees="3"),
        _transaction(2, "Buy to Open", "2026-01-06", quantity="2", price="10", fees="2"),
        _transaction(3, "Sell to Close", "2026-01-07", quantity="1", price="15", fees="1"),
        _transaction(4, "Buy to Close", "2026-01-08", quantity="1", price="15", fees="2"),
    )

    result = replay_fifo(transactions)

    assert [record.direction for record in result.realized_records] == ["long", "short"]
    long_close, short_close = result.realized_records
    assert (long_close.quantity, long_close.open_fees, long_close.close_fees, long_close.pnl) == (
        Decimal("1"), Decimal("1"), Decimal("1"), Decimal("3.0000")
    )
    assert (short_close.quantity, short_close.open_fees, short_close.close_fees, short_close.pnl) == (
        Decimal("1"), Decimal("1"), Decimal("2"), Decimal("2.0000")
    )
    assert {(lot.direction, lot.remaining_quantity) for lot in result.open_lots} == {
        ("long", Decimal("1")),
        ("short", Decimal("2")),
    }


def test_close_quantity_cannot_exceed_available_quantity_in_its_direction() -> None:
    """多头数量不足时拒绝平仓，不拿同标的空头批次补足差额。"""
    transactions = (
        _transaction(1, "Sell to Open", "2026-01-05", quantity="5", price="20", fees="0"),
        _transaction(2, "Buy to Open", "2026-01-06", quantity="1", price="10", fees="0"),
        _transaction(3, "Sell to Close", "2026-01-07", quantity="2", price="15", fees="0"),
    )

    with pytest.raises(FifoError, match="平仓数量 2 超过未平数量 1"):
        replay_fifo(transactions)
