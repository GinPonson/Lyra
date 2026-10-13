"""导入提交适配器的集成测试（真实 DuckDB）。

覆盖写入路径上最要紧的四条性质：

1. **版本冲突必须整体回滚**——只拒绝请求但留下半提交数据，比不拒绝更糟；
2. **FIFO 产物是全量重算**——先删后插，否则会留下过期批次；
3. **无变更不新增账本版本**——版本号是审计信号，凭空递增没有意义；
4. **账户不匹配直接报错**——继续下去会把交易写进别的账户。
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.import_commit import ImportCommitPreview, build_import_commit_plan
from lyra.db.connection import open_lyra_database
from lyra.domain.ledger.types import ParsedTransaction
from lyra.infrastructure.db.import_commit_repository import DuckDbImportCommitRepository

ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
LEGACY_KEY = "TST001"


def _h(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _txn(
    txn_hash: str,
    *,
    action: str = "Buy",
    date: str = "2026-01-05",
    quantity: str = "10",
    price: str = "40",
    fees: str = "0",
    amount: str = "-400",
) -> ParsedTransaction:
    """构造一条标准交易。"""
    return ParsedTransaction(
        account=LEGACY_KEY, source_type="csv", source_reference="src",
        record_date=date, transaction_date=date, action=action,
        raw_symbol="INTC", description="INTEL CORP", asset_type="stock", underlying="INTC",
        expiry=None, strike=None, option_type=None,
        quantity=Decimal(quantity), price=Decimal(price), fees=Decimal(fees),
        amount=Decimal(amount), source_hash=_h("source"), sequence=1, occurrence=1,
        business_hash=_h(txn_hash), transaction_hash=_h(txn_hash),
    )


def _preview(*transactions: ParsedTransaction) -> ImportCommitPreview:
    return ImportCommitPreview(account=LEGACY_KEY, transactions=transactions)


@pytest.fixture()
def wired(tmp_path: Path):
    """在临时真实库上装配仓储与账户。"""
    database = open_lyra_database(str(tmp_path / "commit.duckdb"))
    connection = database.connection
    for account_id, key, suffix in (
        (ACCOUNT_ID, LEGACY_KEY, "001"),
        (OTHER_ACCOUNT_ID, "TST002", "002"),
    ):
        connection.execute(
            """
            INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
            VALUES (CAST(? AS UUID), ?, ?, ?)
            """,
            [account_id, f"Account {key}", key, suffix],
        )
    try:
        yield DuckDbImportCommitRepository(connection), connection
    finally:
        database.close()


def _count(connection, table: str) -> int:
    row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def _commit(repository, plan, expected: int = 0, account_id: str = ACCOUNT_ID):
    return repository.commit_import(
        account_id=account_id,
        plan=plan,
        expected_ledger_version=expected,
        source_reference="src",
    )


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------


def test_commit_inserts_transactions_and_fifo_products(wired) -> None:
    """提交必须写入交易、未平批次、已实现明细与账本版本关联。"""
    repository, connection = wired
    plan = build_import_commit_plan(_preview(_txn("buy")), ())

    result = _commit(repository, plan)

    assert result.changed is True
    assert result.inserted_rows == 1
    assert result.ledger_version == 1
    assert _count(connection, "transactions") == 1
    assert _count(connection, "lots") == 1
    assert _count(connection, "realized") == 0
    assert _count(connection, "ledger_versions") == 1
    assert _count(connection, "ledger_version_accounts") == 1
    # 交易与批次都引用新版本。
    assert connection.execute("SELECT DISTINCT ledger_version FROM transactions").fetchone()[0] == 1
    assert connection.execute("SELECT DISTINCT ledger_version FROM lots").fetchone()[0] == 1


def test_commit_records_realized_when_lot_is_closed(wired) -> None:
    """开平仓配对后必须产生已实现明细且不留未平批次。"""
    repository, connection = wired
    plan = build_import_commit_plan(
        _preview(
            _txn("buy", action="Buy", date="2026-01-05", amount="-400"),
            _txn("sell", action="Sell", date="2026-01-10", price="60", amount="600"),
        ),
        (),
    )

    _commit(repository, plan)

    assert _count(connection, "lots") == 0
    assert _count(connection, "realized") == 1
    row = connection.execute("SELECT pnl FROM realized").fetchone()
    assert row is not None and Decimal(row[0]) == Decimal(200)


def test_version_bump_is_monotonic(wired) -> None:
    """每次有变更提交都推进一个新版本号。"""
    repository, _ = wired
    first = _commit(repository, build_import_commit_plan(_preview(_txn("a")), ()))
    second = _commit(
        repository,
        build_import_commit_plan(_preview(_txn("b", date="2026-01-06")), (_txn("a"),)),
        expected=first.ledger_version,
    )

    assert second.ledger_version == first.ledger_version + 1


# --------------------------------------------------------------------------
# 版本冲突与回滚
# --------------------------------------------------------------------------


def test_version_conflict_leaves_no_partial_write(wired) -> None:
    """版本不符必须报 409，且**一行都不落库**。"""
    repository, connection = wired
    # 先提交一次，使账本版本变成 1。
    _commit(repository, build_import_commit_plan(_preview(_txn("a")), ()))
    before = {t: _count(connection, t) for t in ("transactions", "lots", "ledger_versions")}

    # 带着过期版本（0）再次提交。
    stale = build_import_commit_plan(
        _preview(_txn("b", date="2026-01-06")), (_txn("a"),)
    )
    with pytest.raises(ApplicationError) as raised:
        _commit(repository, stale, expected=0)

    assert raised.value.code == "LEDGER_VERSION_CONFLICT"
    assert raised.value.status_code == 409
    assert raised.value.details == {
        "expected_ledger_version": 0,
        "actual_ledger_version": 1,
    }
    assert {t: _count(connection, t) for t in before} == before


def test_unchanged_plan_does_not_create_a_ledger_version(wired) -> None:
    """无变更提交不推进版本号——版本号是审计信号。"""
    repository, connection = wired
    first = _commit(repository, build_import_commit_plan(_preview(_txn("a")), ()))

    # 用与库中完全相同的交易再提交一次预览。
    same = build_import_commit_plan(_preview(_txn("a")), (_txn("a"),))
    result = _commit(repository, same, expected=first.ledger_version)

    assert result.changed is False
    assert result.ledger_version == first.ledger_version
    assert result.skipped_rows == 1
    assert _count(connection, "ledger_versions") == 1


def test_account_mismatch_is_rejected_before_any_write(wired) -> None:
    """账户 ID 与计划账户键不一致时必须直接报错，不进入事务。"""
    repository, connection = wired
    plan = build_import_commit_plan(_preview(_txn("a")), ())

    with pytest.raises(ValueError, match="不匹配"):
        _commit(repository, plan, account_id=OTHER_ACCOUNT_ID)
    assert _count(connection, "transactions") == 0


# --------------------------------------------------------------------------
# 替换语义
# --------------------------------------------------------------------------


def test_fifo_products_are_replaced_not_appended(wired) -> None:
    """再次提交必须**替换**该账户的 lots/realized，而不是追加。"""
    repository, connection = wired
    first = _commit(repository, build_import_commit_plan(_preview(_txn("a")), ()))
    assert _count(connection, "lots") == 1

    # 追加一笔卖出，使原批次被平掉。
    plan = build_import_commit_plan(
        _preview(_txn("b", action="Sell", date="2026-01-10", price="60", amount="600")),
        (_txn("a"),),
    )
    _commit(repository, plan, expected=first.ledger_version)

    assert _count(connection, "lots") == 0, "旧批次未被删除"
    assert _count(connection, "realized") == 1
    assert _count(connection, "transactions") == 2


def test_replacement_is_scoped_to_one_account(wired) -> None:
    """替换只作用于目标账户，不得波及其它账户的批次。"""
    repository, connection = wired
    # 直接为另一个账户写入一个批次与一条已实现记录。
    # 必须让**序列**分配版本号：显式写 version 不会推进 ledger_version_seq，
    # 下一次 nextval 仍会返回同一个值并撞主键。
    connection.execute(
        """
        INSERT INTO ledger_versions (reason, source_reference)
        VALUES ('file_import', 'other')
        """
    )
    # lots.open_transaction_hash 有指向 transactions 的外键，因此先插入对应交易。
    connection.execute(
        """
        INSERT INTO transactions (
            account_id, record_date, transaction_date, action, raw_symbol, description,
            asset_type, underlying, quantity, price, fees, amount, source_type,
            source_reference, source_hash, sequence, occurrence, business_hash,
            transaction_hash, parser_version, ledger_version
        ) VALUES (
            CAST(? AS UUID), CAST('2026-02-01' AS DATE), CAST('2026-02-01' AS DATE), 'Buy',
            'AMD', 'ADVANCED MICRO', 'stock', 'AMD', CAST('5' AS DECIMAL(18,6)),
            CAST('100' AS DECIMAL(18,6)), CAST('0' AS DECIMAL(18,6)),
            CAST('-500' AS DECIMAL(18,4)), 'csv', 'other', ?, 1, 1, ?, ?, 2, 1
        )
        """,
        [OTHER_ACCOUNT_ID, _h("other-source"), _h("other-business"), _h("other")],
    )
    connection.execute(
        """
        INSERT INTO lots (account_id, symbol_key, asset_type, underlying, direction, open_date,
                          open_price, open_quantity, remaining_quantity, open_fees,
                          fees_allocated, open_transaction_hash, ledger_version)
        VALUES (CAST(? AS UUID), 'AMD', 'stock', 'AMD', 'long', CAST('2026-02-01' AS DATE),
                CAST('100' AS DECIMAL(18,6)), CAST('5' AS DECIMAL(18,6)),
                CAST('5' AS DECIMAL(18,6)), CAST('0' AS DECIMAL(18,6)),
                CAST('0' AS DECIMAL(18,6)), ?, 1)
        """,
        [OTHER_ACCOUNT_ID, _h("other")],
    )

    # 目标是第一个账户，第二个账户的批次必须原样保留。
    _commit(repository, build_import_commit_plan(_preview(_txn("a")), ()), expected=1)

    rows = connection.execute(
        "SELECT account_id, symbol_key FROM lots ORDER BY symbol_key"
    ).fetchall()
    assert len(rows) == 2
    assert {row[1] for row in rows} == {"AMD", "INTC"}


def test_missing_fifo_product_is_rejected(wired) -> None:
    """计划声称有变更却没有 FIFO 产物时必须报错，而不是写半套数据。"""
    repository, connection = wired
    plan = build_import_commit_plan(_preview(_txn("a")), ())
    broken = plan.__class__(**{**plan.__dict__, "fifo": None})

    with pytest.raises(ValueError, match="缺少 FIFO 产物"):
        _commit(repository, broken)
    assert _count(connection, "transactions") == 0
