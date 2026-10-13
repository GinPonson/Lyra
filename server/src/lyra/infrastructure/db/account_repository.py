"""账户的读取与创建。

**创建是原子的一步**：账户行、审计事件与幂等收口必须在同一事务内完成。分开写会出现
"账户已建但审计未记"或"账户已建但幂等记录仍是 in_progress"的中间态——前者让审计
失真，后者让重放读到进行中而实际已完成。
"""

from __future__ import annotations

from datetime import datetime

import duckdb


def _iso_z(value: object) -> str:
    """把数据库时间戳渲染为与创建时一致的 ``...Z`` 形式。

    @param value: 数据库返回的时间戳（``datetime`` 或字符串）。
    @returns ISO 8601 UTC 毫秒字符串。
    """
    from datetime import UTC, datetime

    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return (
            moment.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return str(value)


def load_brokerage_account(
    connection: duckdb.DuckDBPyConnection, account_id: str
) -> dict[str, object] | None:
    """按 ID 读取账户。

    @param connection: 数据库连接。
    @param account_id: 账户 UUID。
    @returns 账户行（内部字段名）；不存在时 ``None``。
    """
    row = connection.execute(
        """
        SELECT id, display_name, broker, masked_suffix, base_currency, status, created_at
        FROM accounts WHERE id = CAST(? AS UUID)
        """,
        [account_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "display_name": row[1],
        "broker": row[2],
        "masked_suffix": row[3],
        "base_currency": row[4],
        "status": row[5],
        # **归一化为与创建时相同的形态**：DuckDB 读回的是带本地偏移的
        # ``2026-10-13T15:48:59.545000+08:00``，而创建时写入的是 ``...545Z``。
        # 不归一化会让**同一个账户在首次创建与重放时返回不同的时间戳字符串**——
        # 客户端若用它做缓存键或比较，会认为这是两个不同的对象。
        "created_at": _iso_z(row[6]),
    }


def persist_created_account(
    connection: duckdb.DuckDBPyConnection,
    *,
    account: dict[str, object],
    audit_event: dict[str, object],
    idempotency_completion: tuple[str, str, int, datetime] | None,
) -> None:
    """原子写入账户、审计事件与幂等收口。

    @param connection: 数据库连接。
    @param account: 账户字段。
    @param audit_event: 审计事件字段。
    @param idempotency_completion: ``(record_id, result_resource_id, status, at)``；
        未携带幂等键时为 ``None``。
    """
    connection.execute("BEGIN TRANSACTION")
    try:
        # **事务内预检重复标识**。依赖表的 UNIQUE 约束也能挡住，但那样的报错是一句
        # 底层的约束信息，客户端拿不到稳定错误码。
        #
        # 比对用 ``upper(trim(...))``：标识在归一化后才入库，但仍以这一步兜住历史数据
        # 中的大小写差异——**已归档账户同样不能重复创建**。
        duplicate = connection.execute(
            """
            SELECT 1 FROM accounts
            WHERE upper(trim(legacy_account_key)) = ? LIMIT 1
            """,
            [str(account["legacy_account_key"]).upper()],
        ).fetchone()
        if duplicate is not None:
            from ...application.app_error import ApplicationError

            raise ApplicationError(
                code="ACCOUNT_ALREADY_EXISTS",
                message="该文件账户标识已存在，请选择已有账户；已归档账户也不能重复创建",
                status_code=409,
                retryable=False,
            )
        connection.execute(
            """
            INSERT INTO accounts
                (id, display_name, legacy_account_key, broker, masked_suffix,
                 base_currency, status, created_at, updated_at)
            VALUES (CAST(? AS UUID), ?, ?, ?, ?, ?, ?, CAST(? AS TIMESTAMPTZ),
                    CAST(? AS TIMESTAMPTZ))
            """,
            [
                account["id"], account["display_name"], account["legacy_account_key"],
                account["broker"], account["masked_suffix"], account["base_currency"],
                account["status"], account["created_at"], account["created_at"],
            ],
        )
        connection.execute(
            """
            INSERT INTO audit_events
                (id, occurred_at, event_type, outcome, actor_type, actor_id,
                 request_id, target_type, target_id, account_id, metadata_json)
            VALUES (CAST(? AS UUID), CAST(? AS TIMESTAMPTZ), ?, ?, 'device',
                    CAST(? AS UUID), ?, ?, ?, CAST(? AS UUID), ?)
            """,
            [
                audit_event["id"], audit_event["occurred_at"], audit_event["event_type"],
                audit_event["outcome"], audit_event["actor_id"],
                audit_event["request_id"], audit_event["target_type"],
                audit_event["target_id"], audit_event["account_id"],
                audit_event["metadata_json"],
            ],
        )
        if idempotency_completion is not None:
            from .mutation_idempotency_repository import (
                complete_mutation_idempotency_record,
            )

            record_id, resource_id, status_code, completed_at = idempotency_completion
            complete_mutation_idempotency_record(
                connection, record_id=record_id, result_resource_id=resource_id,
                response_status_code=status_code, completed_at=completed_at,
            )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
