"""账本一致性审计。

镜像 TypeScript 版 ``audit.ts``。审计**只读**：它不重放、不修复，只报告缺失
字段、未反映交易、孤儿引用与非法批次数量。按既有 Python 行为，**不把已过期
期权当作账本一致性错误**——那是数据质量问题，不是账本自相矛盾。
"""

from __future__ import annotations

from ..fifo.types import FifoReplayResult
from ..ledger.types import ParsedTransaction
from .dates import is_account_in_scope
from .types import AccountScope, LedgerAuditCheck, LedgerAuditResult

# 范围说明文案沿用现有实现的措辞（oracle 侧写作「本命令」，措辞差异不涉及口径）。
SCOPE_NOTE = "按用户要求，本功能不检查已过期期权"


def audit_ledger(
    transactions: tuple[ParsedTransaction, ...],
    fifo: FifoReplayResult,
    account_scope: AccountScope,
) -> LedgerAuditResult:
    """只读审计原始账本与 FIFO 派生结果的一致性。

    @param transactions: 当前标准交易账本。
    @param fifo: 当前已持久化或待持久化的 FIFO 派生结果。
    @param account_scope: 显式账户范围。
    @returns 包含每项检查与总状态的只读报告。
    """
    scoped_transactions = [
        transaction for transaction in transactions
        if is_account_in_scope(transaction.account, account_scope)
    ]
    scoped_lots = [
        lot for lot in fifo.open_lots if is_account_in_scope(lot.account, account_scope)
    ]
    scoped_realized = [
        record for record in fifo.realized_records
        if is_account_in_scope(record.account, account_scope)
    ]

    transaction_hashes = {t.transaction_hash for t in scoped_transactions}
    reflected_hashes: set[str] = set()
    for lot in scoped_lots:
        reflected_hashes.add(lot.open_transaction)
    for record in scoped_realized:
        reflected_hashes.add(record.open_transaction)
        reflected_hashes.add(record.close_transaction)

    position_transactions = [
        transaction for transaction in scoped_transactions
        if transaction.asset_type in ("stock", "option")
    ]
    invalid_trade_fields = sum(
        1 for transaction in position_transactions
        if transaction.raw_symbol is None
        or transaction.underlying is None
        or transaction.quantity is None
        or transaction.quantity <= 0
    )
    unreflected = sum(
        1 for transaction in position_transactions
        if transaction.transaction_hash not in reflected_hashes
    )
    orphan_open_lots = sum(
        1 for lot in scoped_lots if lot.open_transaction not in transaction_hashes
    )
    orphan_realized_rows = sum(
        1 for record in scoped_realized
        if record.open_transaction not in transaction_hashes
        or record.close_transaction not in transaction_hashes
    )
    invalid_lot_quantities = sum(
        1 for lot in scoped_lots
        if lot.open_quantity <= 0
        or lot.remaining_quantity <= 0
        or lot.remaining_quantity > lot.open_quantity
    )
    derivation_missing = (
        1
        if position_transactions and not scoped_lots and not scoped_realized
        else 0
    )

    rows = (
        _presence_check("transactions_present", len(scoped_transactions), "至少一条原始交易"),
        _zero_check("invalid_trade_fields", invalid_trade_fields),
        _zero_check("unreflected_fifo_transactions", unreflected),
        _zero_check("orphan_open_lots", orphan_open_lots),
        _zero_check("orphan_realized_rows", orphan_realized_rows),
        _zero_check("invalid_lot_quantities", invalid_lot_quantities),
        _zero_check("fifo_derivatives_present", derivation_missing),
    )
    failed = sum(1 for row in rows if row.status == "fail")
    dates = sorted(t.transaction_date for t in scoped_transactions)

    return LedgerAuditResult(
        rows=rows,
        ok=failed == 0,
        failed=failed,
        transaction_count=len(scoped_transactions),
        latest_transaction_date=dates[-1] if dates else None,
        read_only=True,
        scope_note=SCOPE_NOTE,
    )


def _presence_check(check: str, count: int, expectation: str) -> LedgerAuditCheck:
    """构造要求至少存在一条记录的检查。"""
    return LedgerAuditCheck(
        check=check,
        severity="error",
        count=count,
        expectation=expectation,
        status="pass" if count > 0 else "fail",
    )


def _zero_check(check: str, count: int) -> LedgerAuditCheck:
    """构造要求异常计数为零的检查。"""
    return LedgerAuditCheck(
        check=check,
        severity="error",
        count=count,
        expectation="0",
        status="pass" if count == 0 else "fail",
    )
