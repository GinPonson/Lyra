"""Gmail 连接与账户映射的持久化。

镜像 TypeScript 版 ``infrastructure/db/gmail-connection-repository.ts``。

Schema 上的三条约束在这里得到体现：

| 约束 | 作用 |
| --- | --- |
| ``UNIQUE (user_id, google_subject)`` | **同一用户**的一个 Google 账号只有一条连接；不同用户可以各自连接同一个 Google 账号 |
| ``status = 'disconnected' OR encrypted_refresh_token IS NOT NULL`` | **未断开的连接必须有令牌**——否则它既不能用也无法重连 |
| ``UNIQUE (connection_id, account_suffix)`` | 同一连接内一个尾号只能映射到一个账户 |

**唯一键按用户划分是这里最关键的一处**（P0 契约 §3.12）：全局 ``google_subject UNIQUE``
意味着 B 授权同一个 Google 账号时会顶掉 A 的连接（A 的同步游标与令牌被 B 覆盖），
而两个用户各自连接自己的工作邮箱是正常用法。P1a 已把唯一键改为
``(user_id, google_subject)``，因此本模块的每一条连接查询/写入都必须带 ``user_id``——
少一处就会读到或改到别人的连接。

映射与消息经 connection 隔离，因此它们的用户范围**由所连接归属间接表达**：查询一律
沿 ``gmail_connections.user_id`` 过滤，插入则用 ``INSERT ... SELECT ... WHERE EXISTS``
在**同一条语句**内确认 connection 与 account 同属该用户——分成两步检查会在两步之间
留下一个可写入的窗口。

映射改绑用**旧账户 ID 做比较并交换**：两台设备并发改同一条映射时，后到的一方收到
冲突而不是静默覆盖先到的一方。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import duckdb

from ...application.app_error import ApplicationError
from ...application.gmail_user_scope import assert_gmail_authorization_active
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
    connection: duckdb.DuckDBPyConnection, user_id: str, record: GmailConnectionRecord
) -> PersistedGmailConnectionRow:
    """插入一条属于指定用户的连接。

    @param connection: 数据库连接。
    @param user_id: 连接归属的用户 UUID 文本；**必填**，不接受 ``None`` 表示全部用户。
    @param record: 待插入的记录。
    @returns 已持久化的行。
    @raises ValueError: 未断开的连接没有 refresh token（违反 schema 不变量）。
    @raises ApplicationError: 该用户已连接同一个 Google 账号（唯一约束冲突）。
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
                id, user_id, google_subject, google_email, encrypted_refresh_token,
                granted_scopes, history_id, status, last_error_code, last_sync_at,
                created_at, updated_at
            ) VALUES (
                CAST(? AS UUID), CAST(? AS UUID), ?, ?, ?, ?, ?, ?, ?, CAST(? AS TIMESTAMPTZ),
                CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ)
            )
            """,
            [
                record.id, user_id, record.google_subject, record.google_email,
                record.encrypted_refresh_token, record.granted_scopes,
                record.history_id, record.status, record.last_error_code,
                record.last_sync_at, record.created_at, record.updated_at,
            ],
        )
    except duckdb.ConstraintException as error:
        # 唯一约束是 ``(user_id, google_subject)``：重复授权只与**同一用户**的既有
        # 连接冲突——同一个 Google 账号在另一个 Lyra 用户下是各自独立的一条连接，
        # 不再互相顶掉（P0 契约 §3.12）。
        #
        # 先确认冲突确实来自这条唯一键再报"已有连接"：外键或 CHECK 违规也会抛
        # ``ConstraintException``，把它们一并说成重复授权会把运维引向错误方向。
        if _has_connection_for_subject(connection, user_id, record.google_subject):
            raise ApplicationError(
                code="GMAIL_CONNECTION_ERROR",
                message="该 Google 账号在当前用户下已有连接",
                status_code=409,
                retryable=False,
            ) from error
        raise
    return _to_row(record)


def _has_connection_for_subject(
    connection: duckdb.DuckDBPyConnection, user_id: str, google_subject: str
) -> bool:
    """该用户下是否已存在同一 Google 账号的连接。

    **只用于错误归因**：唯一约束是否触发仍由数据库判定，这里的查询不参与写入，
    因此不存在"查完再插"的竞态。
    """
    row = connection.execute(
        """
        SELECT 1 FROM gmail_connections
        WHERE user_id = CAST(? AS UUID) AND google_subject = ?
        """,
        [user_id, google_subject],
    ).fetchone()
    return row is not None


def load_gmail_connection_by_subject(
    connection: duckdb.DuckDBPyConnection, user_id: str, google_subject: str
) -> PersistedGmailConnectionRow | None:
    """按 Google subject 读取当前用户的连接。

    @param connection: 数据库连接。
    @param user_id: 当前用户 UUID。
    @param google_subject: 已验证的 Google subject。
    @returns 连接行；没有匹配连接时返回 ``None``。
    """
    row = connection.execute(
        f"""
        SELECT {_COLUMNS} FROM gmail_connections
        WHERE user_id = CAST(? AS UUID) AND google_subject = ?
        """,
        [user_id, google_subject],
    ).fetchone()
    return None if row is None else _row_to_connection(row)


def upsert_gmail_connection_row(
    connection: duckdb.DuckDBPyConnection,
    user_id: str,
    record: GmailConnectionRecord,
    *,
    device_id: str,
) -> PersistedGmailConnectionRow:
    """建立 Gmail 连接，或用新授权凭据更新当前用户的同一 Google 连接。

    更新时保留既有连接 ID、创建时间、同步游标和最近同步时间，因此账户映射与已处理
    消息仍关联同一连接。调用方需先用既有 ID 加密 refresh token，保证 AAD 一致。

    @param connection: 数据库连接。
    @param user_id: 连接归属的用户 UUID。
    @param record: 新授权产生的连接记录。
    @param device_id: 创建授权事务的设备；必须仍活动并属于该用户。
    @returns 已持久化连接行。
    @raises ValueError: 未断开连接缺少 refresh token，或已有连接 ID 与记录不符。
    @raises ApplicationError: 并发首次授权导致同 subject 冲突。
    """
    if record.status != "disconnected" and record.encrypted_refresh_token is None:
        raise ValueError("未断开的 Gmail 连接必须有 refresh token")
    connection.execute("BEGIN TRANSACTION")
    try:
        # OAuth 网络往返期间不能持有数据库事务；在本事务开始后重验设备/用户，
        # 让撤销或停用与凭据写入形成明确的先后顺序。
        assert_gmail_authorization_active(connection, user_id, device_id)
        existing = load_gmail_connection_by_subject(
            connection, user_id, record.google_subject
        )
        if existing is None:
            create_gmail_connection_row(connection, user_id, record)
        else:
            if existing.id != record.id:
                raise ApplicationError(
                    code="GMAIL_CONNECTION_ERROR",
                    message="Gmail 连接刚刚发生变化，请重新授权",
                    status_code=409,
                    retryable=True,
                )
            # DuckDB 的 UPDATE ... RETURNING 在父行被外键引用时会触发 FK 限制，即使
            # 更新的是普通字段。只更新非索引列并省略 RETURNING；同步游标、最近同步时间、
            # 创建时间以及连接 ID 均留在数据库原行上，消息关联和映射自然保持不变。
            connection.execute(
                """
                UPDATE gmail_connections
                SET google_email = ?, encrypted_refresh_token = ?, granted_scopes = ?,
                    status = ?, last_error_code = ?, updated_at = CAST(? AS TIMESTAMPTZ)
                WHERE user_id = CAST(? AS UUID) AND id = CAST(? AS UUID)
                  AND google_subject = ?
                """,
                [
                    record.google_email, record.encrypted_refresh_token,
                    record.granted_scopes, record.status, record.last_error_code,
                    record.updated_at, user_id, record.id, record.google_subject,
                ],
            )

        persisted = load_gmail_connection_by_subject(
            connection, user_id, record.google_subject
        )
        assert persisted is not None
        connection.execute("COMMIT")
        return persisted
    except BaseException:
        # 保持唯一键、外键及历史数据原子性；异常仍原样向上传递。
        connection.execute("ROLLBACK")
        raise


def load_gmail_connections(
    connection: duckdb.DuckDBPyConnection, user_id: str
) -> tuple[PersistedGmailConnectionRow, ...]:
    """按创建时间读取**当前用户**的全部连接。

    **必须带用户范围**：全库读取会把其他用户的连接（含其邮箱与状态）返回给调用方
    （P0 契约 §3.12）。部署级的连接计数另有
    :func:`count_gmail_connections_needing_attention`，不经过本函数。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @returns 按创建时间、ID 排序的连接行。
    """
    rows = connection.execute(
        f"SELECT {_COLUMNS} FROM gmail_connections "
        "WHERE user_id = CAST(? AS UUID) ORDER BY created_at, id",
        [user_id],
    ).fetchall()
    return tuple(_row_to_connection(row) for row in rows)


def count_gmail_connections_needing_attention(connection: duckdb.DuckDBPyConnection) -> int:
    """部署级统计：有多少 Gmail 连接需要处理。

    **只给健康端点用，不返回给客户端。** 多用户下「有多少条连接」本身就是用户活动
    信息，公开端点不得暴露（计划 §4.4、P0 契约 §3.4）；健康响应只据此推导
    ``available`` / ``degraded``。

    **只算 needs_resync 与 error 两种**：其余（含 connected、disconnected）都不影响
    服务健康——把「用户主动断开」算成需要处理会让服务长期停在 degraded。

    @param connection: 数据库连接。
    @returns 需要处理的连接条数。
    """
    row = connection.execute(
        "SELECT count(*) FROM gmail_connections WHERE status IN ('needs_resync', 'error')"
    ).fetchone()
    return 0 if row is None else int(row[0])


def load_gmail_connection(
    connection: duckdb.DuckDBPyConnection, user_id: str, connection_id: str
) -> PersistedGmailConnectionRow | None:
    """按 ID 读取一条**属于当前用户**的连接。

    ``user_id`` 刻意放在 ``connection_id`` **之前**：两个参数都是字符串，顺序写反不会
    报错，只会读到别人的连接；把用户放在前面能让漏传时直接缺参报错（与
    :func:`lyra.infrastructure.db.account_lookup.resolve_legacy_account_key` 同一约定）。

    不属于该用户的连接与不存在的连接返回**同一个 ``None``**，调用方据此返回同一个
    404——区分两者会让攻击者靠状态码枚举出他人连接是否存在。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @param connection_id: 连接 UUID。
    @returns 连接行；不存在或不属于该用户时 ``None``。
    """
    row = connection.execute(
        f"""
        SELECT {_COLUMNS} FROM gmail_connections
        WHERE user_id = CAST(? AS UUID) AND id = CAST(? AS UUID)
        """,
        [user_id, connection_id],
    ).fetchone()
    return None if row is None else _row_to_connection(row)


def save_gmail_connection_row(
    connection: duckdb.DuckDBPyConnection,
    user_id: str,
    record: GmailConnectionRecord,
    *,
    expected_updated_at: str,
) -> None:
    """整体覆盖一条**属于当前用户**的连接的状态字段，以 ``updated_at`` 做比较并交换。

    **不更新身份列**（``google_subject`` / ``google_email``）：连接与 Google 账号的
    绑定在创建后不可变。允许同步过程改写它，就等于允许一次同步把连接悄悄指向另一个
    邮箱——而之后的每封邮件都来自那个新邮箱。

    **CAS 的意义**：同步是长流程（刷新令牌、拉取消息、写账本），期间用户可能在另一台
    设备上断开连接。不加 CAS 的话，这次同步的收尾会把"已断开"覆盖回"活动中"，
    用户会看到连接自己又活了。

    ``user_id`` 条件与 CAS 条件必须同时存在：只有 CAS 时，A 拿到 B 的连接 ID 并读到
    其 ``updated_at`` 就能改写 B 的连接状态。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @param record: 新的连接状态。
    @param expected_updated_at: 调用方读到的 ``updated_at``。
    @raises ApplicationError: 连接不属于该用户、或已被改动（409，可重试）。
    """
    result = connection.execute(
        """
        UPDATE gmail_connections
        SET encrypted_refresh_token = ?, granted_scopes = ?, history_id = ?,
            status = ?, last_error_code = ?, last_sync_at = CAST(? AS TIMESTAMPTZ),
            updated_at = CAST(? AS TIMESTAMPTZ)
        WHERE user_id = CAST(? AS UUID) AND id = CAST(? AS UUID)
          AND updated_at = CAST(? AS TIMESTAMPTZ)
        """,
        [
            record.encrypted_refresh_token, record.granted_scopes, record.history_id,
            record.status, record.last_error_code, record.last_sync_at,
            record.updated_at, user_id, record.id, expected_updated_at,
        ],
    )
    # DuckDB UPDATE ... RETURNING 会在连接行被映射或消息外键引用时触发 FK 限制。
    # 用同一条 CAS UPDATE 的影响行数判断冲突，避免先读后写产生竞态。
    updated = result.fetchone()
    if updated is None or int(updated[0]) != 1:
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
    connection: duckdb.DuckDBPyConnection, user_id: str, connection_id: str
) -> tuple[PersistedGmailAccountMappingRecord, ...]:
    """读取某个**属于当前用户**的连接的全部账户映射，按尾号排序。

    映射表本身没有 ``user_id`` 列（P0 契约 §4.1「Gmail 映射与消息经 connection
    隔离」），因此用户范围由连接归属表达：连接不属于该用户时返回空元组——与"该连接
    没有映射"不可区分，不泄露他人连接是否存在。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @param connection_id: 连接 UUID。
    @returns 该连接的映射行；连接不属于该用户或不存在时为空元组。
    """
    rows = connection.execute(
        """
        SELECT m.id, m.connection_id, m.account_suffix, m.account_id,
               m.created_at, m.updated_at
        FROM gmail_account_mappings m
        JOIN gmail_connections c ON c.id = m.connection_id
        WHERE c.user_id = CAST(? AS UUID) AND m.connection_id = CAST(? AS UUID)
        ORDER BY m.account_suffix
        """,
        [user_id, connection_id],
    ).fetchall()
    return tuple(_row_to_mapping(row) for row in rows)


def load_gmail_account_mapping(
    connection: duckdb.DuckDBPyConnection, user_id: str, mapping_id: str
) -> PersistedGmailAccountMappingRecord | None:
    """按 ID 读取一条**属于当前用户**的映射。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @param mapping_id: 映射 UUID。
    @returns 映射行；不存在或不属于该用户时 ``None``。
    """
    row = connection.execute(
        """
        SELECT m.id, m.connection_id, m.account_suffix, m.account_id,
               m.created_at, m.updated_at
        FROM gmail_account_mappings m
        JOIN gmail_connections c ON c.id = m.connection_id
        WHERE c.user_id = CAST(? AS UUID) AND m.id = CAST(? AS UUID)
        """,
        [user_id, mapping_id],
    ).fetchone()
    return None if row is None else _row_to_mapping(row)


def create_gmail_account_mapping_row(
    connection: duckdb.DuckDBPyConnection,
    user_id: str,
    *,
    connection_id: str,
    account_suffix: str,
    account_id: str,
    at: datetime,
) -> PersistedGmailAccountMappingRecord:
    """建立一条账户映射，**在同一语句内**确认连接与账户同属该用户。

    **为什么必须整体失败而不是留下部分写入**：一条把 A 的连接指向 B 的账户的映射，
    会让 A 收到的邮件把交易写进 B 的账本（或反之）。这类跨归属映射一旦落库，
    后续每一次同步都会继续按它写错账本——因此它必须**一条都不写**，而不是"先建映射、
    稍后再校验账户"。

    **为什么用 ``INSERT ... SELECT ... WHERE EXISTS`` 而不是"先查后插"**：两步之间
    存在一个窗口，另一请求可以在窗口内改掉连接或账户归属；写成一条语句则由数据库
    在一个原子步骤里判定归属，且归属不满足时**没有任何行被写入**。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @param connection_id: 连接 UUID；必须属于该用户。
    @param account_suffix: 账户尾号。
    @param account_id: 目标账户 UUID；必须属于该用户。
    @param at: 服务端可信时间。
    @raises ValueError: 尾号为空。
    @raises ApplicationError: 连接或账户不属于该用户（404），或该连接内尾号已被映射（409）。
    """
    suffix = account_suffix.strip()
    if suffix == "":
        raise ValueError("账户尾号不能为空")
    mapping_id = str(uuid.uuid4())
    timestamp = _iso(at)
    try:
        row = connection.execute(
            """
            INSERT INTO gmail_account_mappings (
                id, connection_id, account_suffix, account_id, created_at, updated_at
            )
            SELECT CAST(? AS UUID), CAST(? AS UUID), ?, CAST(? AS UUID),
                   CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ)
            WHERE EXISTS (
                SELECT 1
                FROM gmail_connections c
                JOIN accounts a ON a.user_id = c.user_id
                WHERE c.user_id = CAST(? AS UUID)
                  AND c.id = CAST(? AS UUID)
                  AND a.id = CAST(? AS UUID)
            )
            RETURNING id
            """,
            [
                mapping_id, connection_id, suffix, account_id, timestamp, timestamp,
                user_id, connection_id, account_id,
            ],
        ).fetchone()
    except duckdb.ConstraintException as error:
        raise ApplicationError(
            code="GMAIL_CONNECTION_ERROR",
            message="该账户尾号已映射到其它账户",
            status_code=409,
            retryable=False,
        ) from error
    if row is None:
        # 归属不满足：连接或账户不属于该用户。与"资源不存在"返回同一个 404 语义，
        # 不区分二者以免被用来枚举他人资源。
        raise ApplicationError(
            code="GMAIL_CONNECTION_ERROR",
            message="Gmail 连接或目标账户不属于当前用户",
            status_code=404,
            retryable=False,
        )
    loaded = load_gmail_account_mapping(connection, user_id, mapping_id)
    assert loaded is not None  # 刚插入必然可读
    return loaded


def update_gmail_account_mapping_row(
    connection: duckdb.DuckDBPyConnection,
    user_id: str,
    *,
    mapping_id: str,
    expected_account_id: str,
    next_account_id: str,
    at: datetime,
) -> None:
    """改绑映射，以旧账户 ID 做比较并交换。

    用户范围与 CAS 必须写在**同一条 UPDATE** 里：映射属于 A 的连接、或目标账户属于
    B 时都不满足条件，从而既改不到别人的映射，也接不上别人的账户。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @param mapping_id: 映射 UUID。
    @param expected_account_id: 调用方读到的当前账户 UUID。
    @param next_account_id: 目标账户 UUID；必须属于该用户。
    @param at: 服务端可信时间。
    @raises ApplicationError: 映射不属于该用户、目标账户不属于该用户、或映射已被其它
        设备改动（409，可重试）。
    """
    row = connection.execute(
        """
        UPDATE gmail_account_mappings
        SET account_id = CAST(? AS UUID), updated_at = CAST(? AS TIMESTAMPTZ)
        WHERE id = CAST(? AS UUID) AND account_id = CAST(? AS UUID)
          AND EXISTS (
              SELECT 1 FROM gmail_connections c
              WHERE c.id = gmail_account_mappings.connection_id
                AND c.user_id = CAST(? AS UUID)
          )
          AND EXISTS (
              SELECT 1 FROM accounts a
              WHERE a.id = CAST(? AS UUID) AND a.user_id = CAST(? AS UUID)
          )
        RETURNING id
        """,
        [
            next_account_id, _iso(at), mapping_id, expected_account_id,
            user_id, next_account_id, user_id,
        ],
    ).fetchone()
    if row is None:
        # CAS 失败：先到的一方已改过。**报冲突而不是覆盖**——静默覆盖会让
        # 先到设备的改动消失，而两台设备都以为保存成功了。
        #
        # 归属不满足同样落到这里：调用方拿到的仍是 409 冲突，不区分"不存在"与
        # "不属于你"，避免用它枚举他人映射。
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
