"""导入写入路径的端到端测试。

前面的测试各自验证一个环节：预览生成、提交计划、幂等判定、仓储写入、读取投影。
**单测各自通过并不代表它们能组合工作**——接缝处的字段名、账户键、版本号、
日期形状都可能在传递中走样。

本文件用一条完整链路把它们串起来：

```
原始八列行 → 预览 → 提交计划 → 单事务提交 → 从库里读回 → 分析层计算
```

任一处不匹配都会在这里暴露，而单元测试不会。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.import_commit import ImportCommitPreview, build_import_commit_plan
from lyra.application.import_preview import (
    CreateImportPreviewInput,
    ImportCommitSnapshot,
    assert_import_preview_current,
    create_import_preview,
)
from lyra.db.connection import open_lyra_database
from lyra.domain.analytics.positions import aggregate_positions
from lyra.domain.analytics.summary import calculate_account_summary
from lyra.domain.analytics.types import AccountScope
from lyra.domain.fifo.replay import replay_fifo
from lyra.domain.ledger.batch import TransactionBatchInput
from lyra.domain.ledger.types import RawTransactionFields
from lyra.infrastructure.db.import_commit_repository import DuckDbImportCommitRepository
from lyra.infrastructure.db.ledger_repository import DuckDbLedgerRepository

ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"
LEGACY_KEY = "TST001"
PARSER_VERSION = "2"
SOURCE_HASH = "a" * 64
USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"


def _row(
    date: str,
    action: str,
    quantity: str,
    price: str,
    amount: str,
    symbol: str = "INTC",
) -> RawTransactionFields:
    """构造一行 Schwab 八列原始数据（金额须与数量价格勾稽）。"""
    return RawTransactionFields(
        date=date, action=action, symbol=symbol, description=f"{symbol} CORP",
        quantity=quantity, price=f"${price}", fees="", amount=amount,
    )


@pytest.fixture()
def ledger(tmp_path: Path):
    """装配真实库、账户与仓储。"""
    database = open_lyra_database(str(tmp_path / "e2e.duckdb"))
    connection = database.connection
    # 账户归属 NOT NULL；账本仓储与提交仓储都绑定同一个用户。
    connection.execute(
        """
        INSERT INTO users (id, display_name, status, role)
        VALUES (CAST(? AS UUID), 'E2E User', 'active', 'member')
        """,
        [USER_ID],
    )
    connection.execute(
        """
        INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Account', ?, '001')
        """,
        [ACCOUNT_ID, USER_ID, LEGACY_KEY],
    )
    try:
        yield (
            connection,
            DuckDbImportCommitRepository(connection, user_id=USER_ID),
            DuckDbLedgerRepository(connection, user_id=USER_ID),
        )
    finally:
        database.close()


def _existing_hashes(connection) -> frozenset[str]:
    """从库里读出目标账户已有的交易哈希，供预览统计跳过数。"""
    rows = connection.execute(
        "SELECT transaction_hash FROM transactions WHERE account_id = CAST(? AS UUID)",
        [ACCOUNT_ID],
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _preview(connection, *rows: RawTransactionFields, base_version: int = 0):
    """按库中现有哈希生成预览。"""
    return create_import_preview(CreateImportPreviewInput(
        batch=TransactionBatchInput(
            file_name="ledger.csv", account=LEGACY_KEY, source_hash=SOURCE_HASH,
            source_type="csv", source_reference="ledger.csv", rows=rows,
        ),
        parser_version=PARSER_VERSION,
        base_ledger_version=base_version,
        existing_transaction_hashes=_existing_hashes(connection),
    ))


def _commit(connection, repository, preview, expected_version: int):
    """按预览生成计划并提交。"""
    plan = build_import_commit_plan(
        ImportCommitPreview(account=preview.account, transactions=preview.transactions),
        tuple(
            DuckDbLedgerRepository(connection, user_id=USER_ID).load_snapshot().transactions
        ),
    )
    return repository.commit_import(
        account_id=ACCOUNT_ID,
        plan=plan,
        expected_ledger_version=expected_version,
        source_reference="ledger.csv",
    )


OPENING = _row("01/05/2026", "Buy", "10", "40.00", "-$400.00")
CLOSING = _row("01/10/2026", "Sell", "10", "60.00", "$600.00")
SECOND_LOT = _row("01/12/2026", "Buy", "5", "50.00", "-$250.00", symbol="AMD")


# --------------------------------------------------------------------------
# 完整链路
# --------------------------------------------------------------------------


def test_preview_plan_commit_read_back(ledger) -> None:
    """一次完整导入：预览 → 计划 → 提交 → 读回 → 分析。"""
    connection, commit_repository, ledger_repository = ledger

    preview = _preview(connection, OPENING, CLOSING)
    assert preview.total_rows == 2
    assert preview.insertable_rows == 2
    assert preview.base_ledger_version == 0
    assert preview.first_transaction_date == "2026-01-05"
    assert preview.last_transaction_date == "2026-01-10"

    result = _commit(connection, commit_repository, preview, expected_version=0)
    assert result.changed is True
    assert result.ledger_version == 1

    snapshot = ledger_repository.load_snapshot()
    # 读回的交易与预览里的**逐字段一致**：接缝处不能改字段。
    assert len(snapshot.transactions) == 2
    assert {t.transaction_hash for t in snapshot.transactions} == {
        t.transaction_hash for t in preview.transactions
    }
    assert {t.account for t in snapshot.transactions} == {LEGACY_KEY}
    # 开平仓配对后无未平批次、有一条已实现记录。
    assert snapshot.open_lots == ()
    assert len(snapshot.realized_records) == 1
    assert snapshot.realized_records[0].pnl == Decimal(200)


def test_imported_ledger_drives_analytics(ledger) -> None:
    """导入的数据必须能直接驱动分析层，而不是只写进库。"""
    connection, commit_repository, ledger_repository = ledger
    _commit(connection, commit_repository, _preview(connection, OPENING, CLOSING), 0)

    snapshot = ledger_repository.load_snapshot()
    fifo = replay_fifo(snapshot.transactions)
    summary = calculate_account_summary(
        snapshot.transactions, fifo, AccountScope(kind="all")
    )

    assert summary.txn_count == 2
    assert summary.realized_pnl_total == "200.0000"
    # cash_balance 是快照内全部交易金额之和：-400 + 600 = +200。
    assert summary.cash_balance == "200.0000"


def test_second_import_appends_and_recomputes_fifo(ledger) -> None:
    """第二次导入必须追加交易并按完整账本重算 FIFO。"""
    connection, commit_repository, ledger_repository = ledger
    first = _commit(connection, commit_repository, _preview(connection, OPENING, CLOSING), 0)

    # 第二次导入：一笔新开仓，且与第一次的导出区间不重叠。
    second_preview = _preview(connection, SECOND_LOT, base_version=first.ledger_version)
    second = _commit(connection, commit_repository, second_preview, first.ledger_version)

    assert second.ledger_version == first.ledger_version + 1
    snapshot = ledger_repository.load_snapshot()
    assert len(snapshot.transactions) == 3
    # 新的 AMD 开仓成为唯一未平批次；INTC 的已实现记录仍在。
    assert [lot.symbol_key for lot in snapshot.open_lots] == ["AMD"]
    assert len(snapshot.realized_records) == 1


def test_reimport_of_same_file_changes_nothing(ledger) -> None:
    """重复导入同一份文件必须是无变更，且账本逐字段不变。"""
    connection, commit_repository, ledger_repository = ledger
    first = _commit(connection, commit_repository, _preview(connection, OPENING, CLOSING), 0)
    before = ledger_repository.load_snapshot()

    # 同一份文件再预览：全部行都已在库中。
    again = _preview(connection, OPENING, CLOSING, base_version=first.ledger_version)
    assert again.insertable_rows == 0
    assert again.existing_rows == 2

    result = _commit(connection, commit_repository, again, first.ledger_version)

    assert result.changed is False
    assert result.ledger_version == first.ledger_version
    after = ledger_repository.load_snapshot()
    assert [t.transaction_hash for t in after.transactions] == [
        t.transaction_hash for t in before.transactions
    ]
    assert [r.pnl for r in after.realized_records] == [r.pnl for r in before.realized_records]


def test_stale_preview_cannot_be_committed(ledger) -> None:
    """提交前复核必须拦下过期预览——否则会把交易提交到已变化的账本上。"""
    connection, commit_repository, _ = ledger
    preview = _preview(connection, OPENING, CLOSING)
    # 帐本在预览后被别的导入推进了。
    _commit(connection, commit_repository, _preview(connection, SECOND_LOT), 0)

    with pytest.raises(ApplicationError) as raised:
        assert_import_preview_current(
            preview,
            ImportCommitSnapshot(
                file_hash=preview.file_hash,
                parser_version=preview.parser_version,
                account=preview.account,
                ledger_version=1,
            ),
        )
    assert raised.value.code == "IMPORT_PREVIEW_STALE"
    assert raised.value.details == {"changed_fields": ["ledger_version"]}


def test_positions_after_partial_import(ledger) -> None:
    """只导入开仓时，持仓必须能反映出来（未平批次写对了）。"""
    connection, commit_repository, ledger_repository = ledger
    _commit(connection, commit_repository, _preview(connection, OPENING), 0)

    snapshot = ledger_repository.load_snapshot()
    positions = aggregate_positions(snapshot.open_lots, AccountScope(kind="all"))

    assert len(positions) == 1
    assert positions[0].symbol_key == "INTC"
    assert positions[0].qty == Decimal(10)
