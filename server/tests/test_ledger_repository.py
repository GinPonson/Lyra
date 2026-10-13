"""账本快照仓储的集成测试。

跑在真实 schema 15 上：插入一批交易、未平批次与已实现记录，再读回领域对象。

重点验证两条形状约定：**日期以 ISO 文本往返**（领域层按文本排序与比较），
以及**账户以脱敏键呈现**（表里是 UUID，领域层用 ``legacy_account_key``）。
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from lyra.db.connection import open_lyra_database
from lyra.domain.analytics.types import AccountScope
from lyra.infrastructure.db.ledger_repository import DuckDbLedgerRepository

ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"


def _h(label: str) -> str:
    """生成 schema 要求的 64 位十六进制哈希；用真实 sha256 以便断言可对照。"""
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _seed(connection) -> None:
    """写入一份最小但完整的账本。"""
    connection.execute(
        "INSERT INTO ledger_versions (version, reason, source_reference) VALUES (1, 'file_import', 'src')"
    )
    for account_id, key, suffix in (
        (ACCOUNT_ID, "TST001", "001"),
        (OTHER_ACCOUNT_ID, "TST002", "002"),
    ):
        connection.execute(
            """
            INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
            VALUES (CAST(? AS UUID), ?, ?, ?)
            """,
            [account_id, f"Account {key}", key, suffix],
        )

    transactions = [
        (ACCOUNT_ID, "2026-01-05", "2026-01-05", "Buy", "INTC", "INTEL CORP", "stock", "INTC",
         "10", "40.00", "-400.00", "buy-1"),
        (ACCOUNT_ID, "2026-01-10", "2026-01-10", "Sell", "INTC", "INTEL CORP", "stock", "INTC",
         "10", "60.00", "600.00", "sell-1"),
        (OTHER_ACCOUNT_ID, "2026-02-01", "2026-02-01", "Buy", "AMD", "ADVANCED MICRO", "stock", "AMD",
         "5", "100.00", "-500.00", "buy-2"),
    ]
    for index, (account_id, record_date, txn_date, action, symbol, description, asset_type,
                underlying, quantity, price, amount, txn_hash) in enumerate(transactions, start=1):
        connection.execute(
            """
            INSERT INTO transactions
                (account_id, record_date, transaction_date, action, raw_symbol, description,
                 asset_type, underlying, quantity, price, amount, source_type, source_reference,
                 source_hash, sequence, occurrence, business_hash, transaction_hash,
                 parser_version, ledger_version)
            VALUES (CAST(? AS UUID), CAST(? AS DATE), CAST(? AS DATE), ?, ?, ?, ?, ?,
                    CAST(? AS DECIMAL(18,6)), CAST(? AS DECIMAL(18,6)), CAST(? AS DECIMAL(18,4)),
                    'csv', 'src', ?, ?, 1, ?, ?, 2, 1)
            """,
            [account_id, record_date, txn_date, action, symbol, description, asset_type,
             underlying, quantity, price, amount, _h("source"), index,
             _h(f"business-{index}"), _h(txn_hash)],
        )

    connection.execute(
        """
        INSERT INTO lots
            (account_id, symbol_key, asset_type, underlying, direction, open_date, open_price,
             open_quantity, remaining_quantity, open_fees, fees_allocated, open_transaction_hash,
             ledger_version)
        VALUES (CAST(? AS UUID), 'AMD', 'stock', 'AMD', 'long', CAST('2026-02-01' AS DATE),
                CAST('100.00' AS DECIMAL(18,6)), CAST('5' AS DECIMAL(18,6)),
                CAST('5' AS DECIMAL(18,6)), CAST('0' AS DECIMAL(18,6)),
                CAST('0' AS DECIMAL(18,6)), ?, 1)
        """,
        [OTHER_ACCOUNT_ID, _h("buy-2")],
    )

    connection.execute(
        """
        INSERT INTO realized
            (account_id, symbol_key, asset_type, underlying, direction, open_date, close_date,
             quantity, open_price, close_price, open_fees, close_fees, pnl, close_action,
             open_transaction_hash, close_transaction_hash, ledger_version)
        VALUES (CAST(? AS UUID), 'INTC', 'stock', 'INTC', 'long', CAST('2026-01-05' AS DATE),
                CAST('2026-01-10' AS DATE), CAST('10' AS DECIMAL(18,6)),
                CAST('40.00' AS DECIMAL(18,6)), CAST('60.00' AS DECIMAL(18,6)),
                CAST('0' AS DECIMAL(18,6)), CAST('0' AS DECIMAL(18,6)),
                CAST('200.0000' AS DECIMAL(18,4)), 'Sell', ?, ?, 1)
        """,
        [ACCOUNT_ID, _h("buy-1"), _h("sell-1")],
    )


@pytest.fixture()
def repository(tmp_path: Path):
    """在临时真实库上装配仓储。"""
    database = open_lyra_database(str(tmp_path / "ledger.duckdb"))
    _seed(database.connection)
    try:
        yield DuckDbLedgerRepository(database.connection)
    finally:
        database.close()


def test_loads_all_transactions_with_legacy_account_key(repository) -> None:
    """交易必须带上脱敏账户键，而不是 UUID。"""
    snapshot = repository.load_snapshot()

    assert len(snapshot.transactions) == 3
    assert {t.account for t in snapshot.transactions} == {"TST001", "TST002"}
    assert all(t.account != ACCOUNT_ID for t in snapshot.transactions)


def test_dates_round_trip_as_iso_text(repository) -> None:
    """日期必须是 ISO 文本；返回 date 对象会让领域层的文本排序失效。"""
    snapshot = repository.load_snapshot()

    for transaction in snapshot.transactions:
        assert isinstance(transaction.record_date, str)
        assert isinstance(transaction.transaction_date, str)
        assert len(transaction.record_date) == 10
    assert snapshot.transactions[0].record_date == "2026-01-05"


def test_transactions_are_sorted_stably(repository) -> None:
    """交易按生效日与文件内顺序稳定排列。"""
    snapshot = repository.load_snapshot()

    dates = [t.transaction_date for t in snapshot.transactions]
    assert dates == sorted(dates)
    assert snapshot.transactions[0].action == "Buy"
    assert snapshot.transactions[1].action == "Sell"


def test_decimals_are_exact(repository) -> None:
    """数值列必须是精确 Decimal，不能经过浮点。"""
    snapshot = repository.load_snapshot()
    buy = snapshot.transactions[0]

    assert buy.quantity == Decimal(10)
    assert buy.price == Decimal("40.00")
    assert buy.amount == Decimal("-400.00")
    assert isinstance(buy.price, Decimal)


def test_loads_open_lots_and_skips_closed_ones(repository) -> None:
    """只读取未平批次；库中不应存在已完全平仓的批次。"""
    snapshot = repository.load_snapshot()

    assert len(snapshot.open_lots) == 1
    lot = snapshot.open_lots[0]
    assert lot.account == "TST002"
    assert lot.symbol_key == "AMD"
    assert lot.remaining_quantity == Decimal(5)
    assert lot.direction == "long"


def test_loads_realized_records(repository) -> None:
    """已实现明细必须完整还原，含开平仓交易哈希。"""
    snapshot = repository.load_snapshot()

    assert len(snapshot.realized_records) == 1
    record = snapshot.realized_records[0]
    assert record.account == "TST001"
    assert record.pnl == Decimal("200.0000")
    assert record.open_transaction == _h("buy-1")
    assert record.close_transaction == _h("sell-1")
    assert record.close_date == "2026-01-10"


def test_account_scope_filters_every_table(repository) -> None:
    """账户范围必须同时作用于交易、批次与已实现明细。"""
    snapshot = repository.load_snapshot(AccountScope(kind="account", account="TST001"))

    assert {t.account for t in snapshot.transactions} == {"TST001"}
    assert snapshot.open_lots == ()
    assert len(snapshot.realized_records) == 1

    other = repository.load_snapshot(AccountScope(kind="account", account="TST002"))
    assert {t.account for t in other.transactions} == {"TST002"}
    assert len(other.open_lots) == 1
    assert other.realized_records == ()


def test_unknown_account_scope_returns_empty(repository) -> None:
    """不存在的账户返回空结果，而不是回落到全部账户。"""
    snapshot = repository.load_snapshot(AccountScope(kind="account", account="NOPE"))

    assert snapshot.transactions == ()
    assert snapshot.open_lots == ()
    assert snapshot.realized_records == ()


def test_snapshot_feeds_analytics_end_to_end(repository) -> None:
    """快照可直接驱动分析层：用读回的数据算总览与回撤。"""
    from lyra.domain.analytics.dates import all_accounts_scope
    from lyra.domain.analytics.risk import calculate_drawdown
    from lyra.domain.analytics.summary import calculate_account_summary

    snapshot = repository.load_snapshot()
    fifo = _replay(snapshot)

    summary = calculate_account_summary(snapshot.transactions, fifo, all_accounts_scope())
    assert summary.txn_count == 3
    assert summary.cash_balance == "-300.0000"

    drawdown = calculate_drawdown(fifo.realized_records, all_accounts_scope())
    assert drawdown.ending_cumulative_pnl == "200.0000"


def _replay(snapshot):
    """对快照执行 FIFO 重放。"""
    from lyra.domain.fifo.replay import replay_fifo

    return replay_fifo(snapshot.transactions)


def test_loads_accounts_with_base_currency(repository) -> None:
    """快照必须带上账户身份与币种：公开响应按币种分组，跨币种相加是错的。"""
    snapshot = repository.load_snapshot()

    assert [account.legacy_account_key for account in snapshot.accounts] == ["TST001", "TST002"]
    assert all(account.base_currency == "USD" for account in snapshot.accounts)
    # 账户以内部 UUID 标识，供公开响应的账户引用使用。
    assert {account.id for account in snapshot.accounts} == {ACCOUNT_ID, OTHER_ACCOUNT_ID}


def test_account_scope_filters_accounts_too(repository) -> None:
    """账户范围必须同样作用于账户列表，否则公开投影会带出别的账户。"""
    snapshot = repository.load_snapshot(AccountScope(kind="account", account="TST001"))

    assert [account.legacy_account_key for account in snapshot.accounts] == ["TST001"]
