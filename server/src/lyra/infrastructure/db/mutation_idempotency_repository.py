"""设备级变更幂等记录。

镜像 TypeScript 版 ``infrastructure/db/mutation-idempotency-repository.ts``。

**幂等键的作用域是「设备 + 操作 + 键哈希」**（schema 的 UNIQUE 约束）。同一台设备在
同一操作上重复提交同一个键时，只有第一次会真正执行；后续请求读回已有结果。

三条状态纪律，各自防一种错误：

| 纪律 | 不遵守会怎样 |
| --- | --- |
| **完成以 ``status='in_progress'`` 做 CAS** | 两个并发请求都"完成"同一条记录，后写的覆盖先写的 |
| **释放只删 ``in_progress``** | 已完成的记录被删掉，幂等保证随之消失——重放会真的再执行一次 |
| **过期记录先删再插** | 新请求撞上 UNIQUE 约束，用户被一个已过期的键永久挡住 |

schema 还有一条把状态与字段绑定的 CHECK：``completed`` 必须有结果、2xx 状态码与完成
时间。**部分写入的"完成"不可能被误认为已完成。**
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import duckdb

# **复用应用层的记录类型**，不另立一份：两份结构相同的定义迟早会分叉，而分叉时
# 一边加了字段、另一边没有，症状是"读出来的记录少了一个字段"。
# 应用层与基础设施层之间的这一定向依赖在本项目里已有先例（``app_error``）。
from ...application.idempotent_mutation import (
    MutationIdempotencyRecord,
)

__all__ = [
    "MutationIdempotencyRecord",
    "complete_mutation_idempotency_record",
    "load_mutation_idempotency_record",
    "new_idempotency_record",
    "release_mutation_idempotency_record",
    "start_mutation_idempotency_record",
]


def load_mutation_idempotency_record(
    connection: duckdb.DuckDBPyConnection,
    *,
    device_id: str,
    operation: str,
    key_hash: str,
) -> MutationIdempotencyRecord | None:
    """按（设备、操作、键哈希）读取记录。

    @returns 记录；不存在时为 ``None``。
    """
    row = connection.execute(
        """
        SELECT CAST(id AS VARCHAR), CAST(device_id AS VARCHAR), operation,
               key_hash, request_hash, status, result_resource_id,
               response_status_code, created_at, expires_at, completed_at
        FROM mutation_idempotency_records
        WHERE device_id = CAST(? AS UUID) AND operation = ? AND key_hash = ?
        """,
        [device_id, operation, key_hash],
    ).fetchone()
    return None if row is None else _to_record(row)


def start_mutation_idempotency_record(
    connection: duckdb.DuckDBPyConnection,
    *,
    record: MutationIdempotencyRecord,
    replaces_expired_record_id: str | None = None,
) -> None:
    """登记一条 ``in_progress`` 记录。

    **过期记录必须先删。** ``UNIQUE (device_id, operation, key_hash)`` 会让新请求撞上
    那条旧记录——用户会被一个**已过期的键永久挡住**，而重试永远不成功。

    @param connection: 数据库连接。
    @param record: 新记录（``status`` 会被写为 ``in_progress``）。
    @param replaces_expired_record_id: 要替换掉的过期记录 ID。
    """
    if replaces_expired_record_id is not None:
        connection.execute(
            "DELETE FROM mutation_idempotency_records WHERE id = CAST(? AS UUID)",
            [replaces_expired_record_id],
        )
    connection.execute(
        """
        INSERT INTO mutation_idempotency_records (
            id, device_id, operation, key_hash, request_hash, status,
            result_resource_id, response_status_code,
            created_at, expires_at, completed_at
        ) VALUES (
            CAST(? AS UUID), CAST(? AS UUID), ?, ?, ?, 'in_progress',
            NULL, NULL, CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ), NULL
        )
        """,
        [
            record.id, record.device_id, record.operation, record.key_hash,
            record.request_hash, record.created_at, record.expires_at,
        ],
    )


def complete_mutation_idempotency_record(
    connection: duckdb.DuckDBPyConnection,
    *,
    record_id: str,
    result_resource_id: str,
    response_status_code: int,
    completed_at: datetime,
) -> None:
    """把记录收口为 ``completed``。

    **以 ``status='in_progress'`` 做 CAS。** 两个并发请求都"完成"同一条记录时，
    只有一方能写入；后到的一方收到错误而不是**静默覆盖先写的那个结果**——
    否则客户端可能拿到一个指向错误资源的 ID。

    @raises ValueError: 完成时间不带时区，或记录不在 ``in_progress`` 状态。
    """
    if not isinstance(completed_at, datetime) or completed_at.tzinfo is None:
        raise ValueError("幂等完成时间必须带时区")
    row = connection.execute(
        """
        UPDATE mutation_idempotency_records
        SET status = 'completed', result_resource_id = ?,
            response_status_code = ?, completed_at = CAST(? AS TIMESTAMPTZ)
        WHERE id = CAST(? AS UUID) AND status = 'in_progress'
        RETURNING id
        """,
        [
            result_resource_id, response_status_code, _iso(completed_at), record_id,
        ],
    ).fetchone()
    if row is None:
        raise ValueError("幂等记录完成收口失败")


def release_mutation_idempotency_record(
    connection: duckdb.DuckDBPyConnection, record_id: str
) -> None:
    """释放一条 ``in_progress`` 记录（失败路径回滚时用）。

    **只删 ``in_progress``。** 已完成的记录被删掉会让幂等保证消失——**重放会真的
    再执行一次**，而那正是幂等键要防的事。

    @param connection: 数据库连接。
    @param record_id: 记录 ID。
    """
    connection.execute(
        """
        DELETE FROM mutation_idempotency_records
        WHERE id = CAST(? AS UUID) AND status = 'in_progress'
        """,
        [record_id],
    )


def new_idempotency_record(
    *,
    device_id: str,
    operation: str,
    key_hash: str,
    request_hash: str,
    created_at: datetime,
    ttl_seconds: int,
) -> MutationIdempotencyRecord:
    """构造一条待登记的记录。

    @param ttl_seconds: 有效期秒数；必须为正，否则记录的 ``expires_at`` 会早于
        ``created_at``，撞上 schema 的 CHECK。
    @raises ValueError: TTL 非正或时间不带时区。
    """
    from datetime import timedelta

    if ttl_seconds <= 0:
        raise ValueError("幂等记录有效期必须是正秒数")
    if not isinstance(created_at, datetime) or created_at.tzinfo is None:
        raise ValueError("幂等记录创建时间必须带时区")
    return MutationIdempotencyRecord(
        id=str(uuid.uuid4()), device_id=device_id, operation=operation,
        key_hash=key_hash, request_hash=request_hash, status="in_progress",
        result_resource_id=None, response_status_code=None,
        created_at=_iso(created_at),
        expires_at=_iso(created_at + timedelta(seconds=ttl_seconds)),
        completed_at=None,
    )


def _to_record(row: tuple) -> MutationIdempotencyRecord:
    """把查询结果转为记录。"""
    return MutationIdempotencyRecord(
        id=row[0], device_id=row[1], operation=row[2], key_hash=row[3],
        request_hash=row[4], status=row[5], result_resource_id=row[6],
        response_status_code=row[7], created_at=_iso(row[8]),
        expires_at=_iso(row[9]),
        completed_at=None if row[10] is None else _iso(row[10]),
    )


def _iso(value: object) -> str:
    """渲染为与 JavaScript ``toISOString()`` 同形的文本。"""
    if isinstance(value, datetime):
        return (
            value.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return str(value)
