"""Gmail 消息的持久化。

镜像 TypeScript 版 ``infrastructure/db/gmail-message-repository.ts`` 的
状态读取与失败归档部分。

两条值得注意的行为：

1. **``content_hash`` 为 NULL 的行视为"没有已处理状态"**。这让该类行可以被重新
   处理——它是 schema 允许 ``discovered`` 状态但尚未落地的情形留下的防御。
   实测确认：``discovered`` 在参考实现里**从未被写入**，因此这条分支当前不可达，
   但保留它比让它抛错安全。
2. **消息行是普通 INSERT（无 upsert）**。主键是 ``(connection_id, message_id)``，
   因此对同一封邮件重复归档会撞主键。是否可达取决于调用方是否会在重试路径上
   重复调用——这一点已用测试固定下来，避免以后无意中依赖某个未言明的顺序。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import duckdb

from ...infrastructure.gmail.message_import_plan import ExistingGmailMessageState
from ...infrastructure.gmail.message_metadata import ParsedGmailMessageMetadata
from .import_commit_repository import DuckDbImportCommitRepository


def load_existing_gmail_message_state(
    connection: duckdb.DuckDBPyConnection, connection_id: str, message_id: str
) -> ExistingGmailMessageState | None:
    """读取某封邮件已处理的状态。

    **返回领域对象而不是 dict**：导入计划需要 `ExistingGmailMessageState`。
    TypeScript 侧是结构化接口，普通对象即可满足；Python 没有这个性质，返回 dict
    会让组合时报 `AttributeError`。

    @param connection: 数据库连接。
    @param connection_id: 连接 ID。
    @param message_id: 消息 ID。
    @returns 已有状态；**没有行或 ``content_hash`` 为空时返回 ``None``**——
        后者表示该行还没有可比较的内容状态，应按"未处理"对待。
    """
    row = connection.execute(
        """
        SELECT content_hash, CAST(account_id AS VARCHAR), parse_status
        FROM gmail_messages
        WHERE connection_id = CAST(? AS UUID) AND message_id = ?
        """,
        [connection_id, message_id],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return ExistingGmailMessageState(
        content_hash=row[0], account_id=row[1], parse_status=row[2]
    )


def persist_unsuccessful_gmail_message(
    connection: duckdb.DuckDBPyConnection,
    metadata: ParsedGmailMessageMetadata,
    *,
    synced_at: str,
) -> None:
    """归档一条解析拒绝或账户冲突的消息。

    **不含正文**：只写稳定错误码、摘要与身份。

    @param connection: 数据库连接。
    @param metadata: 元数据（``parse_status`` 为 rejected 或 conflict）。
    @param synced_at: 服务端可信时间。
    """
    insert_gmail_message_row(connection, metadata, synced_at=synced_at)


def insert_gmail_message_row(
    connection: duckdb.DuckDBPyConnection,
    metadata: ParsedGmailMessageMetadata,
    *,
    synced_at: str,
) -> None:
    """插入一行消息归档。

    @raises duckdb.ConstraintException: 同一 ``(connection_id, message_id)``
        已存在（主键冲突），或违反 schema 的内容摘要 CHECK。
    """
    connection.execute(
        """
        INSERT INTO gmail_messages (
            connection_id, message_id, thread_id, received_at, content_hash,
            account_id, parse_status, error_code, transaction_count, history_id,
            synced_at
        ) VALUES (
            CAST(? AS UUID), ?, ?, CAST(? AS TIMESTAMPTZ), ?, CAST(? AS UUID),
            ?, ?, ?, ?, CAST(? AS TIMESTAMPTZ)
        )
        """,
        [
            metadata.connection_id, metadata.message_id, metadata.thread_id,
            metadata.received_at, metadata.content_hash, metadata.account_id,
            metadata.parse_status, metadata.error_code,
            metadata.transaction_count, metadata.history_id, synced_at,
        ],
    )


@dataclass(frozen=True)
class PersistedGmailMessageImportResult:
    """一次 Gmail 消息导入的提交结果。"""

    changed: bool
    ledger_version: int
    inserted_rows: int
    skipped_rows: int


def persist_gmail_message_import(
    connection: duckdb.DuckDBPyConnection,
    *,
    plan: Any,
    expected_ledger_version: int,
    synced_at: str,
) -> PersistedGmailMessageImportResult:
    """原子提交一次 Gmail 消息导入。

    **账本变更与消息归档必须在同一个事务里。** 分开提交会出现"交易已入账但消息
    没归档"的中间态——重同步时那封邮件会被当成新邮件**再次导入**，用户看到重复交易。

    @param connection: 数据库连接。
    @param plan: 就绪的导入计划（``kind == "ready"``）。
    @param expected_ledger_version: 同步时读到的账本版本。
    @param synced_at: 服务端可信时间。
    @returns 提交结果。
    @raises ValueError: 计划缺少账户归属或 FIFO 产物。
    @raises ApplicationError: 账本版本已变化（409）。
    """
    account_id = plan.metadata.account_id
    if account_id is None:
        # 已导入的消息必须有账户归属；没有说明上游映射环节出了问题。
        raise ValueError("已导入 Gmail 消息缺少账户归属")

    repository = DuckDbImportCommitRepository(connection)
    connection.execute("BEGIN TRANSACTION")
    try:
        result = repository.write_ledger_changes(
            account_id=account_id, plan=plan.commit,
            expected_ledger_version=expected_ledger_version,
            source_reference=plan.metadata.message_id,
            # 审计时需要区分交易来自文件还是邮件。
            reason="gmail_import",
        )
        insert_gmail_message_row(connection, plan.metadata, synced_at=synced_at)
        for link in plan.links:
            connection.execute(
                """
                INSERT INTO gmail_message_transactions (
                    connection_id, message_id, account_id, transaction_hash, ordinal
                ) VALUES (
                    CAST(? AS UUID), ?, CAST(? AS UUID), ?, ?
                )
                """,
                [
                    link.connection_id, link.message_id, link.account_id,
                    link.transaction_hash, link.ordinal,
                ],
            )
        connection.execute("COMMIT")
        return PersistedGmailMessageImportResult(
            changed=result.changed, ledger_version=result.ledger_version,
            inserted_rows=result.inserted_rows, skipped_rows=result.skipped_rows,
        )
    except Exception:
        connection.execute("ROLLBACK")
        raise
