"""导入提交的 DuckDB 适配器。

镜像 TypeScript 版 ``import-commit-repository.persistImportCommit`` 的核心：
**再校验账本版本 + 单事务插入交易并替换该账户的 lots / realized**。

调用方必须已在单写队列内。本函数自己开事务，失败整体回滚——绝不留下半提交的
账本（只有新交易没有对应批次，会让后续分析算出错误持仓）。

一处非直觉的 DuckDB 约束：**不允许更新已被外键引用的父行**，即使主键不变。
实测代价：只要 ``transactions.import_file_id`` 指向某次导入作业，之后对该作业行的
任何更新（包括写 ``status='committed'`` 或清空预览摘要）都会抛
``ConstraintException: ... is still referenced by a foreign key``。

因此本模块要接管 ``import_files`` 终态写入时，**必须把它排在 ``transactions`` 插入
之前，并且与账本写入处于同一个事务**——这正是 ``preview_ready → committing`` 的 CAS
也应当搬进来的原因（当前 CAS 仍在应用层、事务之外，见
:mod:`lyra.application.import_commit_service` 的说明与恢复路径）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import duckdb

from ...application.app_error import ApplicationError
from ...application.import_commit import ImportCommitPlan
from ...domain.fifo.types import Lot, RealizedRecord
from ...domain.ledger.types import ParsedTransaction
from .mutation_idempotency_repository import complete_mutation_idempotency_record

# 解析器版本：与统一八列标准化内核一致。
#
# 必须是**字符串**：它同时参与预览指纹（作为文本拼接）并写入 VARCHAR 列。
# 用整数会让指纹计算与 API 校验在类型上不一致——这一处曾经写成 int 并导致
# 预览接口直接抛 AttributeError。
PARSER_VERSION = "2"


@dataclass(frozen=True)
class IdempotencyCompletion:
    """幂等记录的收口信息。

    @param record_id: 待收口的记录 ID。
    @param result_resource_id: 本次操作产生的资源 ID（提交时为导入作业 ID）。
    @param response_status_code: 返回给客户端的状态码；schema 要求为 2xx。
    @param completed_at: 完成时间。
    """

    record_id: str
    result_resource_id: str
    response_status_code: int
    completed_at: datetime


@dataclass(frozen=True)
class PersistedImportCommitResult:
    """提交结果。"""

    changed: bool
    ledger_version: int
    inserted_rows: int
    skipped_rows: int


class DuckDbImportCommitRepository:
    """把导入提交计划写入账本。

    @param connection: 已迁移到当前 schema 的连接。
    """

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection

    def commit_import(
        self,
        *,
        account_id: str,
        plan: ImportCommitPlan,
        expected_ledger_version: int,
        source_reference: str,
        idempotency_completion: IdempotencyCompletion | None = None,
    ) -> PersistedImportCommitResult:
        """原子提交一次导入。

        @param account_id: 目标账户的内部 UUID。
        @param plan: 事务外已算好的提交计划。
        @param expected_ledger_version: 预览时读到的账本版本。
        @param source_reference: 记入账本版本的来源引用（导入作业 UUID）。
        @param idempotency_completion: 幂等记录的收口信息；``None`` 表示本次请求
            未携带幂等键。
        @returns 是否变更与新账本版本。
        @raises ApplicationError: 账本版本已变化（409）或账户不可用（409），
            必须重新生成预览。
        """
        self._assert_account_matches(account_id, plan.account)
        self._connection.execute("BEGIN TRANSACTION")
        try:
            result = self.write_ledger_changes(
                account_id=account_id,
                plan=plan,
                expected_ledger_version=expected_ledger_version,
                source_reference=source_reference,
            )
            if idempotency_completion is not None:
                # **收口必须在同一个事务里。** 分开提交会出现"交易已入账但幂等记录仍是
                # in_progress"的中间态：重放时读到 in_progress 而作业其实已完成，
                # 客户端会以为请求仍在进行中，于是继续等或重试。
                complete_mutation_idempotency_record(
                    self._connection,
                    record_id=idempotency_completion.record_id,
                    result_resource_id=idempotency_completion.result_resource_id,
                    response_status_code=idempotency_completion.response_status_code,
                    completed_at=idempotency_completion.completed_at,
                )
            self._connection.execute("COMMIT")
            return result
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def write_ledger_changes(
        self,
        *,
        account_id: str,
        plan: ImportCommitPlan,
        expected_ledger_version: int,
        source_reference: str,
        reason: str = "file_import",
    ) -> PersistedImportCommitResult:
        """把账本变更写入当前事务，**不提交也不回滚**。

        调用方持有事务边界。这样 Gmail 导入可以把账本变更与消息归档放在**同一个
        事务**里——分开提交会出现"交易已入账但消息没归档"的中间态，重同步时那封
        邮件会被当成新邮件再次导入。

        @param account_id: 目标账户的内部 UUID。
        @param plan: 提交计划。
        @param expected_ledger_version: 预览时读到的账本版本。
        @param source_reference: 记入账本版本的来源引用。
        @param reason: 账本版本的原因，区分来源（``file_import`` / ``gmail_import``）。
        @returns 变更结果。
        @raises ApplicationError: 账本版本已变化（409）。
        """
        current = self._current_ledger_version()
        if current != expected_ledger_version:
            raise ApplicationError(
                code="LEDGER_VERSION_CONFLICT",
                message="账本版本已变化，必须重新生成导入预览",
                status_code=409,
                retryable=False,
                details={
                    "expected_ledger_version": expected_ledger_version,
                    "actual_ledger_version": current,
                },
            )

        if not plan.changed:
            # 无变更就不新增账本版本：版本号本身是审计信号，凭空递增没有意义。
            return PersistedImportCommitResult(
                changed=False,
                ledger_version=current,
                inserted_rows=0,
                skipped_rows=plan.skipped_rows,
            )

        if plan.fifo is None:
            raise ValueError("变更导入计划缺少 FIFO 产物")

        ledger_version = self._create_ledger_version(source_reference, reason=reason)
        for transaction in plan.inserted_transactions:
            self._insert_transaction(transaction, account_id, ledger_version)
        # 先删后插：FIFO 产物是整个账户的**全量**重算结果，增量合并会留下过期批次。
        self._connection.execute(
            "DELETE FROM realized WHERE account_id = CAST(? AS UUID)", [account_id]
        )
        self._connection.execute(
            "DELETE FROM lots WHERE account_id = CAST(? AS UUID)", [account_id]
        )
        for lot in plan.fifo.open_lots:
            self._insert_lot(lot, account_id, ledger_version)
        for realized in plan.fifo.realized_records:
            self._insert_realized(realized, account_id, ledger_version)
        self._connection.execute(
            """
            INSERT INTO ledger_version_accounts (ledger_version, account_id)
            VALUES (?, CAST(? AS UUID))
            """,
            [ledger_version, account_id],
        )
        return PersistedImportCommitResult(
            changed=True,
            ledger_version=ledger_version,
            inserted_rows=plan.inserted_rows,
            skipped_rows=plan.skipped_rows,
        )

    def _assert_account_matches(self, account_id: str, legacy_key: str) -> None:
        """确认账户 ID 仍指向计划中的账户，且**账户处于 active**。

        归档账户不得再接收新交易：它已经从会计范围里消失，写入的数据会被所有查询
        忽略——用户看到"导入成功"却查不到任何交易。

        @raises ApplicationError: 账户不存在、键不匹配或已归档（409）。
        """
        row = self._connection.execute(
            "SELECT legacy_account_key, status FROM accounts WHERE id = CAST(? AS UUID)",
            [account_id],
        ).fetchone()
        if row is None or row[0] != legacy_key or row[1] != "active":
            raise ApplicationError(
                code="ACCOUNT_MAPPING_REQUIRED",
                message="导入目标账户已变化，请重新确认账户映射",
                status_code=409,
                retryable=False,
            )

    def _current_ledger_version(self) -> int:
        """读取当前最大账本版本；空账本为 0。"""
        row = self._connection.execute(
            "SELECT coalesce(max(version), 0) FROM ledger_versions"
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _create_ledger_version(self, source_reference: str, *, reason: str) -> int:
        """登记一个新的账本版本并返回其版本号。

        @param source_reference: 来源引用（文件名或消息 ID）。
        @param reason: 来源类型；审计时需要区分交易来自文件还是邮件。
        """
        row = self._connection.execute(
            """
            INSERT INTO ledger_versions (reason, source_reference)
            VALUES (?, ?) RETURNING version
            """,
            [reason, source_reference],
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _insert_transaction(
        self, transaction: ParsedTransaction, account_id: str, ledger_version: int
    ) -> None:
        """插入一条标准交易。

        **``import_file_id`` 目前写 NULL**（见模块 docstring）：一旦交易引用了
        ``import_files``，后续对该作业行的任何更新都会被 DuckDB 的外键限制拒绝；
        要让这一列有值，必须先把作业终态写入搬进本事务、并排在交易插入之前。
        """
        self._connection.execute(
            """
            INSERT INTO transactions (
                account_id, record_date, transaction_date, action, raw_symbol, description,
                asset_type, underlying, expiry, strike, option_type, quantity, price, fees,
                amount, source_type, source_reference, source_hash, sequence, occurrence,
                business_hash, transaction_hash, parser_version, ledger_version
            ) VALUES (
                CAST(? AS UUID), CAST(? AS DATE), CAST(? AS DATE), ?, ?, ?, ?, ?,
                CAST(? AS DATE), CAST(? AS DECIMAL(18,4)), ?,
                CAST(? AS DECIMAL(18,6)), CAST(? AS DECIMAL(18,6)), CAST(? AS DECIMAL(18,6)),
                CAST(? AS DECIMAL(18,4)), ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                account_id, transaction.record_date, transaction.transaction_date,
                transaction.action, transaction.raw_symbol, transaction.description,
                transaction.asset_type, transaction.underlying, transaction.expiry,
                _num(transaction.strike), transaction.option_type,
                _num(transaction.quantity), _num(transaction.price), _num(transaction.fees),
                _num(transaction.amount), transaction.source_type,
                transaction.source_reference, transaction.source_hash,
                transaction.sequence, transaction.occurrence, transaction.business_hash,
                transaction.transaction_hash, PARSER_VERSION, ledger_version,
            ],
        )

    def _insert_lot(self, lot: Lot, account_id: str, ledger_version: int) -> None:
        """插入一个未平批次。"""
        self._connection.execute(
            """
            INSERT INTO lots (
                account_id, symbol_key, asset_type, underlying, expiry, strike, option_type,
                direction, open_date, open_price, open_quantity, remaining_quantity,
                open_fees, fees_allocated, open_transaction_hash, ledger_version
            ) VALUES (
                CAST(? AS UUID), ?, ?, ?, CAST(? AS DATE), CAST(? AS DECIMAL(18,4)), ?,
                ?, CAST(? AS DATE), CAST(? AS DECIMAL(18,6)), CAST(? AS DECIMAL(18,6)),
                CAST(? AS DECIMAL(18,6)), CAST(? AS DECIMAL(18,6)), CAST(? AS DECIMAL(18,6)),
                ?, ?
            )
            """,
            [
                account_id, lot.symbol_key, lot.asset_type, lot.underlying, lot.expiry,
                _num(lot.strike), lot.option_type, lot.direction, lot.open_date,
                _num(lot.open_price), _num(lot.open_quantity), _num(lot.remaining_quantity),
                _num(lot.open_fees), _num(lot.fees_allocated), lot.open_transaction,
                ledger_version,
            ],
        )

    def _insert_realized(
        self, realized: RealizedRecord, account_id: str, ledger_version: int
    ) -> None:
        """插入一条已实现明细。"""
        self._connection.execute(
            """
            INSERT INTO realized (
                account_id, symbol_key, asset_type, underlying, direction, open_date,
                close_date, quantity, open_price, close_price, open_fees, close_fees, pnl,
                close_action, open_transaction_hash, close_transaction_hash, ledger_version
            ) VALUES (
                CAST(? AS UUID), ?, ?, ?, ?, CAST(? AS DATE), CAST(? AS DATE),
                CAST(? AS DECIMAL(18,6)), CAST(? AS DECIMAL(18,6)), CAST(? AS DECIMAL(18,6)),
                CAST(? AS DECIMAL(18,6)), CAST(? AS DECIMAL(18,6)),
                CAST(? AS DECIMAL(18,4)), ?, ?, ?, ?
            )
            """,
            [
                account_id, realized.symbol_key, realized.asset_type, realized.underlying,
                realized.direction, realized.open_date, realized.close_date,
                _num(realized.quantity), _num(realized.open_price),
                _num(realized.close_price), _num(realized.open_fees),
                _num(realized.close_fees), _num(realized.pnl), realized.close_action,
                realized.open_transaction, realized.close_transaction, ledger_version,
            ],
        )


def _num(value: Decimal | None) -> Decimal | None:
    """保持 None 语义（现金交易的数量/价格本就为空）。"""
    return value
