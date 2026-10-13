"""Gmail 连接与账户映射的持久化。

镜像 TypeScript 版 ``infrastructure/db/gmail-connection-repository.ts``。

Schema 上的三条约束在这里得到体现：

| 约束 | 作用 |
| --- | --- |
| ``google_subject`` UNIQUE | 一个 Google 账号只有一条连接（重复授权应更新而非新增） |
| ``status = 'disconnected' OR encrypted_refresh_token IS NOT NULL`` | **未断开的连接必须有令牌**——否则它既不能用也无法重连 |
| ``UNIQUE (connection_id, account_suffix)`` | 同一连接内一个尾号只能映射到一个账户 |

映射改绑用**旧账户 ID 做比较并交换**：两台设备并发改同一条映射时，后到的一方收到
冲突而不是静默覆盖先到的一方。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import duckdb

from ...application.app_error import ApplicationError
from ...infrastructure.gmail.connection_credential import GmailConnectionRecord


@dataclass(frozen=True)
class PersistedGmailConnectionRow:
    """数据库中的连接行。"""

    id: str
    google_subject: str
    google_email: str
    encrypted_refresh_token: str | None
    granted_scopes: str
    history_id: str | None
    status: str
    last_error_code: str | None
    last_sync_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PersistedGmailAccountMappingRecord:
    """数据库中的映射行。"""

    id: str
    connection_id: str
    account_suffix: str
    account_id: str
    created_at: str
    updated_at: str


_COLUMNS = (
    "id, google_subject, google_email, encrypted_refresh_token, granted_scopes, "
    "history_id, status, last_error_code, last_sync_at, created_at, updated_at"
)


def create_gmail_connection_row(
    connection: duckdb.DuckDBPyConnection, record: GmailConnectionRecord
) -> PersistedGmailConnectionRow:
    """插入一条连接。

    @param connection: 数据库连接。
    @param record: 待插入的记录。
    @returns 已持久化的行。
    @raises ValueError: 未断开的连接没有 refresh token（违反 schema 不变量）。
    @raises ApplicationError: Google 账号已有连接（唯一约束冲突）。
    """
    # **显式校验再插入。** 否则这条 CHECK 违规会和唯一约束一样抛
    # ``ConstraintException``，被下面的分支误报成"账号已有连接"——把运维引向
    # 完全错误的方向。
    if record.status != "disconnected" and record.encrypted_refresh_token is None:
        raise ValueError("未断开的 Gmail 连接必须有 refresh token")
    try:
        connection.execute(
            """
            INSERT INTO gmail_connections (
                id, google_subject, google_email, encrypted_refresh_token,
                granted_scopes, history_id, status, last_error_code, last_sync_at,
                created_at, updated_at
            ) VALUES (
                CAST(? AS UUID), ?, ?, ?, ?, ?, ?, ?, CAST(? AS TIMESTAMPTZ),
                CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ)
            )
            """,
            [
                record.id, record.google_subject, record.google_email,
                record.encrypted_refresh_token, record.granted_scopes,
                record.history_id, record.status, record.last_error_code,
                record.last_sync_at, record.created_at, record.updated_at,
            ],
        )
    except duckdb.ConstraintException as error:
        # 唯一约束：一个 Google 账号只有一条连接。重复授权应走"更新已有连接"，
        # 而不是新增一条——否则同一账号会出现两条连接，同步游标互相覆盖。
        raise ApplicationError(
            code="GMAIL_CONNECTION_ERROR",
            message="该 Google 账号已有连接",
            status_code=409,
            retryable=False,
        ) from error
    return _to_row(record)


def load_gmail_connections(
    connection: duckdb.DuckDBPyConnection,
) -> tuple[PersistedGmailConnectionRow, ...]:
    """按创建时间读取全部连接。"""
    rows = connection.execute(
        f"SELECT {_COLUMNS} FROM gmail_connections ORDER BY created_at, id"
    ).fetchall()
    return tuple(_row_to_connection(row) for row in rows)


def load_gmail_connection(
    connection: duckdb.DuckDBPyConnection, connection_id: str
) -> PersistedGmailConnectionRow | None:
    """按 ID 读取一条连接。"""
    row = connection.execute(
        f"SELECT {_COLUMNS} FROM gmail_connections WHERE id = CAST(? AS UUID)",
        [connection_id],
    ).fetchone()
    return None if row is None else _row_to_connection(row)


def save_gmail_connection_row(
    connection: duckdb.DuckDBPyConnection,
    record: GmailConnectionRecord,
    *,
    expected_updated_at: str,
) -> None:
    """整体覆盖一条连接的状态字段，以 ``updated_at`` 做比较并交换。

    **不更新身份列**（``google_subject`` / ``google_email``）：连接与 Google 账号的
    绑定在创建后不可变。允许同步过程改写它，就等于允许一次同步把连接悄悄指向另一个
    邮箱——而之后的每封邮件都来自那个新邮箱。

    **CAS 的意义**：同步是长流程（刷新令牌、拉取消息、写账本），期间用户可能在另一台
    设备上断开连接。不加 CAS 的话，这次同步的收尾会把"已断开"覆盖回"活动中"，
    用户会看到连接自己又活了。

    @param connection: 数据库连接。
    @param record: 新的连接状态。
    @param expected_updated_at: 调用方读到的 ``updated_at``。
    @raises ApplicationError: 连接已被改动（409，可重试）。
    """
    row = connection.execute(
        """
        UPDATE gmail_connections
        SET encrypted_refresh_token = ?, granted_scopes = ?, history_id = ?,
            status = ?, last_error_code = ?, last_sync_at = CAST(? AS TIMESTAMPTZ),
            updated_at = CAST(? AS TIMESTAMPTZ)
        WHERE id = CAST(? AS UUID) AND updated_at = CAST(? AS TIMESTAMPTZ)
        RETURNING id
        """,
        [
            record.encrypted_refresh_token, record.granted_scopes, record.history_id,
            record.status, record.last_error_code, record.last_sync_at,
            record.updated_at, record.id, expected_updated_at,
        ],
    ).fetchone()
    if row is None:
        raise ApplicationError(
            code="GMAIL_CONNECTION_ERROR",
            message="Gmail 连接或映射已变化，请刷新后重试",
            status_code=409,
            retryable=True,
        )


# ---------------------------------------------------------------------------
# 账户映射
# ---------------------------------------------------------------------------


def load_gmail_account_mappings(
    connection: duckdb.DuckDBPyConnection, connection_id: str
) -> tuple[PersistedGmailAccountMappingRecord, ...]:
    """读取某个连接的全部账户映射，按尾号排序。"""
    rows = connection.execute(
        """
        SELECT id, connection_id, account_suffix, account_id, created_at, updated_at
        FROM gmail_account_mappings
        WHERE connection_id = CAST(? AS UUID)
        ORDER BY account_suffix
        """,
        [connection_id],
    ).fetchall()
    return tuple(_row_to_mapping(row) for row in rows)


def load_gmail_account_mapping(
    connection: duckdb.DuckDBPyConnection, mapping_id: str
) -> PersistedGmailAccountMappingRecord | None:
    """按 ID 读取一条映射。"""
    row = connection.execute(
        """
        SELECT id, connection_id, account_suffix, account_id, created_at, updated_at
        FROM gmail_account_mappings WHERE id = CAST(? AS UUID)
        """,
        [mapping_id],
    ).fetchone()
    return None if row is None else _row_to_mapping(row)


def create_gmail_account_mapping_row(
    connection: duckdb.DuckDBPyConnection,
    *,
    connection_id: str,
    account_suffix: str,
    account_id: str,
    at: datetime,
) -> PersistedGmailAccountMappingRecord:
    """建立一条账户映射。

    @raises ValueError: 尾号为空。
    @raises ApplicationError: 该连接内尾号已被映射（唯一约束冲突）。
    """
    suffix = account_suffix.strip()
    if suffix == "":
        raise ValueError("账户尾号不能为空")
    mapping_id = str(uuid.uuid4())
    timestamp = _iso(at)
    try:
        connection.execute(
            """
            INSERT INTO gmail_account_mappings (
                id, connection_id, account_suffix, account_id, created_at, updated_at
            ) VALUES (
                CAST(? AS UUID), CAST(? AS UUID), ?, CAST(? AS UUID),
                CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ)
            )
            """,
            [mapping_id, connection_id, suffix, account_id, timestamp, timestamp],
        )
    except duckdb.ConstraintException as error:
        raise ApplicationError(
            code="GMAIL_CONNECTION_ERROR",
            message="该账户尾号已映射到其它账户",
            status_code=409,
            retryable=False,
        ) from error
    loaded = load_gmail_account_mapping(connection, mapping_id)
    assert loaded is not None  # 刚插入必然可读
    return loaded


def update_gmail_account_mapping_row(
    connection: duckdb.DuckDBPyConnection,
    *,
    mapping_id: str,
    expected_account_id: str,
    next_account_id: str,
    at: datetime,
) -> None:
    """改绑映射，以旧账户 ID 做比较并交换。

    @raises ApplicationError: 映射已被其它设备改动（409，可重试）。
    """
    row = connection.execute(
        """
        UPDATE gmail_account_mappings
        SET account_id = CAST(? AS UUID), updated_at = CAST(? AS TIMESTAMPTZ)
        WHERE id = CAST(? AS UUID) AND account_id = CAST(? AS UUID)
        RETURNING id
        """,
        [next_account_id, _iso(at), mapping_id, expected_account_id],
    ).fetchone()
    if row is None:
        # CAS 失败：先到的一方已改过。**报冲突而不是覆盖**——静默覆盖会让
        # 先到设备的改动消失，而两台设备都以为保存成功了。
        raise ApplicationError(
            code="GMAIL_CONNECTION_ERROR",
            message="Gmail 映射已变化，请刷新后重试",
            status_code=409,
            retryable=True,
        )


def _to_row(record: GmailConnectionRecord) -> PersistedGmailConnectionRow:
    """把领域记录转换为持久化行。"""
    return PersistedGmailConnectionRow(
        id=record.id, google_subject=record.google_subject,
        google_email=record.google_email,
        encrypted_refresh_token=record.encrypted_refresh_token,
        granted_scopes=record.granted_scopes, history_id=record.history_id,
        status=record.status, last_error_code=record.last_error_code,
        last_sync_at=record.last_sync_at, created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _row_to_connection(row: tuple) -> PersistedGmailConnectionRow:
    """把查询结果转换为连接行。"""
    return PersistedGmailConnectionRow(
        id=str(row[0]), google_subject=row[1], google_email=row[2],
        encrypted_refresh_token=row[3], granted_scopes=row[4], history_id=row[5],
        status=row[6], last_error_code=row[7],
        last_sync_at=None if row[8] is None else _iso(row[8]),
        created_at=_iso(row[9]), updated_at=_iso(row[10]),
    )


def _row_to_mapping(row: tuple) -> PersistedGmailAccountMappingRecord:
    """把查询结果转换为映射行。"""
    return PersistedGmailAccountMappingRecord(
        id=str(row[0]), connection_id=str(row[1]), account_suffix=row[2],
        account_id=str(row[3]), created_at=_iso(row[4]), updated_at=_iso(row[5]),
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
