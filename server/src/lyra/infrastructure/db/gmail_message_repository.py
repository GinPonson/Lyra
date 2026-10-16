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

**用户范围**：消息表本身没有 ``user_id``（P0 契约 §4.1「Gmail 映射与消息经 connection
隔离」），因此每条查询/写入都沿 ``gmail_connections.user_id`` 过滤。这也决定了
去重语义：主键是 ``(connection_id, message_id)``，两个用户各自连接 Gmail 后收到
**同一封邮件**（相同 ``message_id``）时各产生一行，**不互相去重**——去重键里的
connection 已经隐式带上用户，重复的两条属于两个不同的账本。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import duckdb

from ...application.app_error import ApplicationError
from ...application.gmail_user_scope import assert_gmail_user_active
from ...infrastructure.gmail.message_import_plan import ExistingGmailMessageState
from ...infrastructure.gmail.message_metadata import ParsedGmailMessageMetadata
from .account_lookup import resolve_legacy_account_key
from .import_commit_repository import DuckDbImportCommitRepository


def load_existing_gmail_message_state(
    connection: duckdb.DuckDBPyConnection,
    user_id: str,
    connection_id: str,
    message_id: str,
) -> ExistingGmailMessageState | None:
    """读取某封邮件已处理的状态。

    **返回领域对象而不是 dict**：导入计划需要 `ExistingGmailMessageState`。
    TypeScript 侧是结构化接口，普通对象即可满足；Python 没有这个性质，返回 dict
    会让组合时报 `AttributeError`。

    查询沿连接归属限定用户：不属于该用户的连接返回 ``None``，与"该邮件没有状态"
    不可区分。少了这一层，A 只要猜到 B 的连接 ID 与邮件 ID 就能读到 B 的账户归属与
    解析状态。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @param connection_id: 连接 ID；必须属于该用户。
    @param message_id: 消息 ID。
    @returns 已有状态；**没有行、连接不属于该用户或 ``content_hash`` 为空时返回
        ``None``**——后者表示该行还没有可比较的内容状态，应按"未处理"对待。
    """
    row = connection.execute(
        """
        SELECT m.content_hash, CAST(m.account_id AS VARCHAR), m.parse_status
        FROM gmail_messages m
        JOIN gmail_connections c ON c.id = m.connection_id
        WHERE c.user_id = CAST(? AS UUID)
          AND m.connection_id = CAST(? AS UUID) AND m.message_id = ?
        """,
        [user_id, connection_id, message_id],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return ExistingGmailMessageState(
        content_hash=row[0], account_id=row[1], parse_status=row[2]
    )


def persist_unsuccessful_gmail_message(
    connection: duckdb.DuckDBPyConnection,
    user_id: str,
    metadata: ParsedGmailMessageMetadata,
    *,
    synced_at: str,
) -> None:
    """归档一条解析拒绝或账户冲突的消息。

    **不含正文**：只写稳定错误码、摘要与身份。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @param metadata: 元数据（``parse_status`` 为 rejected 或 conflict）。
    @param synced_at: 服务端可信时间。
    """
    insert_gmail_message_row(connection, user_id, metadata, synced_at=synced_at)


def insert_gmail_message_row(
    connection: duckdb.DuckDBPyConnection,
    user_id: str,
    metadata: ParsedGmailMessageMetadata,
    *,
    synced_at: str,
) -> None:
    """插入一行消息归档，**在同一语句内**确认连接与账户同属该用户。

    归属谓词写在 ``INSERT ... SELECT`` 里而不是先查后插：两步之间的窗口足以让归属
    在检查后被改掉，而一条语句里归属不满足就是**一行都不写**。这也是"跨归属映射
    不留部分写入"在消息侧的对应保证——只有当连接属于该用户、且（若有）账户也属于
    该用户时，消息行才会落库。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @param metadata: 消息元数据。
    @param synced_at: 服务端可信时间。
    @raises ApplicationError: 连接或账户不属于该用户（404）。
    @raises duckdb.ConstraintException: 同一 ``(connection_id, message_id)``
        已存在（主键冲突），或违反 schema 的内容摘要 CHECK。
    """
    values = [
        metadata.connection_id, metadata.message_id, metadata.thread_id,
        metadata.received_at, metadata.content_hash, metadata.account_id,
        metadata.parse_status, metadata.error_code,
        metadata.transaction_count, metadata.history_id, synced_at,
        metadata.connection_id, user_id,
    ]
    account_predicate = "TRUE"
    if metadata.account_id is not None:
        # 消息行若带账户归属，该账户必须与连接同属一个用户；否则 A 的邮件会把交易
        # 指向 B 的账户。
        account_predicate = (
            "EXISTS (SELECT 1 FROM accounts a "
            "WHERE a.id = CAST(? AS UUID) AND a.user_id = c.user_id)"
        )
        values.append(metadata.account_id)
    row = connection.execute(
        f"""
        INSERT INTO gmail_messages (
            connection_id, message_id, thread_id, received_at, content_hash,
            account_id, parse_status, error_code, transaction_count, history_id,
            synced_at
        )
        SELECT
            CAST(? AS UUID), ?, ?, CAST(? AS TIMESTAMPTZ), ?, CAST(? AS UUID),
            ?, ?, ?, ?, CAST(? AS TIMESTAMPTZ)
        FROM gmail_connections c
        WHERE c.id = CAST(? AS UUID) AND c.user_id = CAST(? AS UUID)
          AND {account_predicate}
        RETURNING message_id
        """,
        values,
    ).fetchone()
    if row is None:
        raise ApplicationError(
            code="GMAIL_CONNECTION_ERROR",
            message="Gmail 连接或目标账户不属于当前用户",
            status_code=404,
            retryable=False,
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
    user_id: str,
    plan: Any,
    expected_ledger_version: int,
    synced_at: str,
) -> PersistedGmailMessageImportResult:
    """原子提交一次 Gmail 消息导入。

    **账本变更与消息归档必须在同一个事务里。** 分开提交会出现"交易已入账但消息
    没归档"的中间态——重同步时那封邮件会被当成新邮件**再次导入**，用户看到重复交易。

    **用户活动状态在同一事务内再查一次**：同步是长流程（刷新令牌、拉取消息、写账本），
    请求在排队或正在拉取期间，用户可能已被停用（计划 §4.4「停用提交后，排队任务实际
    开始执行及后续工具调用必须拒绝该用户」）。放到事务外检查会留下"检查通过 → 停用
    提交 → 仍然入账"的窗口；放在事务内则停用与该写入在**共享连接的显式事务锁**上排序
    （``BEGIN`` 持有连接锁直到 ``COMMIT``/``ROLLBACK``），因此停用先提交就一定读得到
    ``disabled``，而本次事务已先拿到锁时停用会等它结束——那属于"已提交的事务不回滚"。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @param plan: 就绪的导入计划（``kind == "ready"``）。
    @param expected_ledger_version: 同步时读到的**该用户**账本版本。
    @param synced_at: 服务端可信时间。
    @returns 提交结果。
    @raises ValueError: 计划缺少账户归属或 FIFO 产物。
    @raises ApplicationError: 用户已停用（403）或账本版本已变化（409）。
    """
    account_id = plan.metadata.account_id
    if account_id is None:
        # 已导入的消息必须有账户归属；没有说明上游映射环节出了问题。
        raise ValueError("已导入 Gmail 消息缺少账户归属")

    repository = DuckDbImportCommitRepository(connection, user_id=user_id)
    connection.execute("BEGIN TRANSACTION")
    try:
        # 入账前的最后一道闸门：停用若已提交，本事务必然读到 disabled。
        assert_gmail_user_active(connection, user_id)
        # 目标账户必须属于当前用户（``resolve_legacy_account_key`` 已带用户范围），
        # 且仍是计划绑定的那个账户——否则这次写入会落到别人的账本上。P3 的
        # ``write_ledger_changes`` 只按 account_id 写交易，账户归属必须在这里确认。
        legacy_key = resolve_legacy_account_key(connection, user_id, account_id)
        if legacy_key is None or legacy_key != plan.commit.account:
            raise ApplicationError(
                code="ACCOUNT_MAPPING_REQUIRED",
                message="导入目标账户已变化，请重新确认账户映射",
                status_code=409,
                retryable=False,
            )
        result = repository.write_ledger_changes(
            account_id=account_id, plan=plan.commit,
            expected_ledger_version=expected_ledger_version,
            source_reference=plan.metadata.message_id,
            # 审计时需要区分交易来自文件还是邮件。
            reason="gmail_import",
        )
        insert_gmail_message_row(
            connection, user_id, plan.metadata, synced_at=synced_at
        )
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
