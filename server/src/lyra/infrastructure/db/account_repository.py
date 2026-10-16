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
    connection: duckdb.DuckDBPyConnection, user_id: str, account_id: str
) -> dict[str, object] | None:
    """按 ID 读取账户，**且限定在当前用户范围内**。

    ``user_id`` 放在 ``account_id`` 之前：两者都是字符串，顺序写反不会报错，只会读到
    别人的账户。不属于该用户的账户返回 ``None``，与「不存在」不可区分——调用方据此
    返回同一个 404，而不是用空账户掩盖越权（计划 §4.2）。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @param account_id: 账户 UUID。
    @returns 账户行（内部字段名）；不存在或不属于该用户时 ``None``。
    """
    row = connection.execute(
        """
        SELECT id, display_name, broker, masked_suffix, base_currency, status, created_at
        FROM accounts
        WHERE user_id = CAST(? AS UUID) AND id = CAST(? AS UUID)
        """,
        [user_id, account_id],
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
    @param account: 账户字段；**必须带 ``user_id``**（归属列 NOT NULL）。
    @param audit_event: 审计事件字段。
    @param idempotency_completion: ``(record_id, result_resource_id, status, at)``；
        未携带幂等键时为 ``None``。
    """
    connection.execute("BEGIN TRANSACTION")
    try:
        # **事务内预检重复标识，且按用户比对**。依赖表的 ``UNIQUE (user_id,
        # legacy_account_key)`` 也能挡住，但那样的报错是一句底层的约束信息，客户端拿不到
        # 稳定错误码。
        #
        # 比对用 ``upper(trim(...))``：标识在归一化后才入库，但仍以这一步兜住历史数据
        # 中的大小写差异——**已归档账户同样不能重复创建**。
        #
        # **范围必须是当前用户**：唯一键已从全局改为用户内唯一，不带 user_id 会让 B 的
        # 同名标识把 A 的创建请求挡成 409——A/B 用相同标识共存正是本次改造的目标。
        duplicate = connection.execute(
            """
            SELECT 1 FROM accounts
            WHERE user_id = CAST(? AS UUID)
              AND upper(trim(legacy_account_key)) = ? LIMIT 1
            """,
            [account["user_id"], str(account["legacy_account_key"]).upper()],
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
                (id, user_id, display_name, legacy_account_key, broker, masked_suffix,
                 base_currency, status, created_at, updated_at)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), ?, ?, ?, ?, ?, ?,
                    CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ))
            """,
            [
                account["id"], account["user_id"], account["display_name"],
                account["legacy_account_key"], account["broker"],
                account["masked_suffix"], account["base_currency"], account["status"],
                account["created_at"], account["created_at"],
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
