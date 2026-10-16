"""用户与登录身份的 DuckDB 仓储。

把多用户账本里的「用户目录」落到 schema 16 的 ``users`` / ``user_identities`` 两张表上。
这是**共享基础设施仓储**，不是按用户构造的业务仓储：它按显式身份参数工作，管理路由
再用它读取用户；业务资源仓储仍必须在构造时要求明确的 ``user_id``（计划 §4.2）。

**身份键始终是已验证的 ``(provider, subject)``**：邮箱与昵称只作展示，永不用于把两个
身份关联到同一自然人（计划 §4.3）。

**时间戳一律以 ISO UTC 文本进出**（毫秒 + ``Z``），与既有仓储同形：领域层按文本比较
时间，若这里返回 ``datetime`` 对象，比较会静默失败。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

import duckdb

from ...application.app_error import ApplicationError
from ...infrastructure.auth.login_provider import LoginIdentity, LoginProviderId

UserRole = Literal["admin", "member"]
UserStatus = Literal["active", "disabled"]

# 管理端用户列表一次最多返回的用户数。越界**抛错**而不是静默截断：静默截断会让调用方
# 把「被截断的一页」当成完整结果，从而漏掉后面的用户。
MAX_USER_PAGE_SIZE = 200

# 事务嵌套深度按**连接**记录，而不是按仓储实例：同一个连接上会存在多个 UserRepository
# 实例（登录服务、管理路由各持一个），只有按连接计数才能让内层调用加入外层事务。
# 这不是可有可无的优化——DuckDB 在事务内再执行 BEGIN 会直接报错**并中止外层事务**
# （已实测），所以重复 BEGIN 会静默毁掉调用方正在进行的写入。
_transaction_depth: WeakKeyDictionary[object, int] = WeakKeyDictionary()

_USER_COLUMNS = "id, display_name, status, role, created_at, updated_at"

_IDENTITY_SELECT = """
SELECT
    users.id, users.display_name, users.status, users.role,
    users.created_at, users.updated_at,
    user_identities.id, user_identities.user_id, user_identities.provider,
    user_identities.subject, user_identities.email, user_identities.display_name,
    user_identities.created_at, user_identities.updated_at
FROM user_identities
JOIN users ON users.id = user_identities.user_id
"""


@dataclass(frozen=True)
class UserRecord:
    """一个用户的当前状态。

    @param id: 用户 UUID（文本）。
    @param display_name: 展示名称，非空。
    @param status: ``active`` 或 ``disabled``。
    @param role: ``admin`` 或 ``member``。
    @param created_at: 创建时间，ISO UTC 毫秒 + ``Z`` 文本。
    @param updated_at: 最近更新时间，同形文本。
    """

    id: str
    display_name: str
    status: UserStatus
    role: UserRole
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class UserIdentityRecord:
    """一条已验证的来源身份及其归属。

    @param id: 身份 UUID（文本）。
    @param user_id: 所属用户 UUID（文本）。
    @param provider: 登录来源标识。
    @param subject: 该来源下已验证的不可变主体标识。
    @param email: 可选展示邮箱；不参与身份关联。
    @param display_name: 可选展示名。
    @param created_at: 创建时间，ISO UTC 毫秒 + ``Z`` 文本。
    @param updated_at: 最近更新时间，同形文本。
    """

    id: str
    user_id: str
    provider: LoginProviderId
    subject: str
    email: str | None
    display_name: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ResolvedUser:
    """一次登录解析的结果。

    @param user: 解析到的用户。
    @param identity: 本次用于解析的身份。
    @param created: 本次是否新建了用户；重放或重复登录为 ``False``。
    """

    user: UserRecord
    identity: UserIdentityRecord
    created: bool


class UserRepository:
    """基于 DuckDB 连接实现的用户目录仓储。

    @param connection: 已迁移到当前 schema 的连接；写操作由调用方的单写队列串行化。
    """

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """在一个事务内执行后续写入；失败即回滚。

        **可重入**：已经处于本仓储（同一连接）开启的事务中时直接加入外层事务，既不重复
        ``BEGIN``，也不在这里提交或回滚。登录链路要求「用户、身份、设备与凭据签发在一个
        事务中完成」，调用方因此应当用它开启登录事务，再在其中调用本仓储的写入方法。

        @raises Exception: 原样抛出业务异常；若回滚本身也失败，则抛出聚合信息。
        """
        depth = _transaction_depth.get(self._connection, 0)
        if depth > 0:
            _transaction_depth[self._connection] = depth + 1
            try:
                yield
            finally:
                _transaction_depth[self._connection] = depth
            return

        self._connection.execute("BEGIN TRANSACTION")
        _transaction_depth[self._connection] = 1
        try:
            yield
        except Exception as error:
            try:
                self._connection.execute("ROLLBACK")
            except Exception as rollback_error:  # noqa: BLE001
                raise RuntimeError(
                    f"用户事务失败且回滚失败: {error!r} / 回滚错误: {rollback_error!r}"
                ) from error
            raise
        else:
            self._connection.execute("COMMIT")
        finally:
            _transaction_depth[self._connection] = 0

    def find_or_create_user_for_identity(
        self,
        identity: LoginIdentity,
        *,
        now: datetime,
    ) -> ResolvedUser:
        """按 ``(provider, subject)`` 查找用户，未命中则原子创建普通用户与身份。

        新建用户固定为 ``role='member'``、``status='active'``：自动注册不得自行获得
        管理员角色（计划 §4.3）。已存在身份时**只读返回**，不更新身份映射，也不刷新
        邮箱或昵称——归属不因再次登录而转移。

        并发收敛（对应 V02「两个独立登录事务并发完成」）：
        1. 快路径直接查 ``(provider, subject)``，命中即返回；
        2. 未命中则在**同一事务**内重查一次，仍未命中才插入候选用户；
        3. 身份插入用 ``ON CONFLICT (provider, subject) DO NOTHING RETURNING id``：
           冲突时返回空结果集，说明本次被并发事务抢先；
        4. 被抢先就 ``DELETE`` 掉刚建的候选用户（它还没有任何子引用，实测可删）并
           重新读取既有归属，从而不会残留「只有用户、没有身份」的半成品。

        @param identity: 已验证的来源身份。
        @param now: 本次操作时间，用于写入时间戳。
        @returns 解析结果；``created`` 表示本次是否新建了用户。
        @raises ApplicationError: 并发收敛失败（冲突行读不回来），``retryable=True``。
        """
        existing = self._load_identity(identity.provider, identity.subject)
        if existing is not None:
            return ResolvedUser(user=existing[0], identity=existing[1], created=False)

        with self.transaction():
            # 事务内重查：同一进程的并发调用由写协调器串行化，这一步能把绝大多数
            # 「另一个请求刚好先提交」的情况挡在候选用户之外，避免建了又删。
            existing = self._load_identity(identity.provider, identity.subject)
            if existing is not None:
                return ResolvedUser(user=existing[0], identity=existing[1], created=False)

            stamp = _iso(now)
            candidate_user_id = str(uuid4())
            self._insert_user(
                user_id=candidate_user_id,
                display_name=_display_name_for(identity),
                status="active",
                role="member",
                stamp=stamp,
            )
            claimed = self._insert_identity(
                user_id=candidate_user_id,
                identity=identity,
                stamp=stamp,
            )
            if not claimed:
                # 被抢先：候选用户没有任何子引用，删除它是安全的；随后读取真正的归属。
                self._connection.execute(
                    "DELETE FROM users WHERE id = CAST(? AS UUID)", [candidate_user_id]
                )
                existing = self._load_identity(identity.provider, identity.subject)
                if existing is None:
                    raise ApplicationError(
                        code="USER_IDENTITY_CONFLICT",
                        message="登录身份归属正在被其他请求创建，请重试",
                        status_code=409,
                        retryable=True,
                    )
                return ResolvedUser(user=existing[0], identity=existing[1], created=False)

            user = self._load_user(candidate_user_id)
            loaded = self._load_identity(identity.provider, identity.subject)
            if user is None or loaded is None:
                raise ApplicationError(
                    code="USER_IDENTITY_CONFLICT",
                    message="登录身份归属写入后无法读回，请重试",
                    status_code=409,
                    retryable=True,
                )
            return ResolvedUser(user=user, identity=loaded[1], created=True)

    def create_admin(self, identity: LoginIdentity, *, now: datetime) -> ResolvedUser:
        """离线初始化入口用：创建固定 ``role='admin'`` 的用户与身份。

        与自动注册的关键差别是**不静默降级**：同一 ``(provider, subject)`` 已经存在
        用户时直接失败，而不是返回一个 ``member`` 用户冒充初始化成功（计划 §9）。

        @param identity: 显式确认的首个管理员身份。
        @param now: 本次操作时间，用于写入时间戳。
        @returns 新建的管理员解析结果，``created`` 恒为 ``True``。
        @raises ApplicationError: 该身份已存在用户（409），``details`` 携带既有用户 id 与角色。
        """
        with self.transaction():
            existing = self._load_identity(identity.provider, identity.subject)
            if existing is not None:
                raise _identity_exists_error(existing[0])

            stamp = _iso(now)
            candidate_user_id = str(uuid4())
            self._insert_user(
                user_id=candidate_user_id,
                display_name=_display_name_for(identity),
                status="active",
                role="admin",
                stamp=stamp,
            )
            claimed = self._insert_identity(
                user_id=candidate_user_id,
                identity=identity,
                stamp=stamp,
            )
            if not claimed:
                self._connection.execute(
                    "DELETE FROM users WHERE id = CAST(? AS UUID)", [candidate_user_id]
                )
                raced = self._load_identity(identity.provider, identity.subject)
                if raced is None:
                    raise ApplicationError(
                        code="USER_IDENTITY_CONFLICT",
                        message="管理员身份归属正在被其他请求创建，请重试",
                        status_code=409,
                        retryable=True,
                    )
                raise _identity_exists_error(raced[0])

            user = self._load_user(candidate_user_id)
            loaded = self._load_identity(identity.provider, identity.subject)
            if user is None or loaded is None:
                raise ApplicationError(
                    code="USER_IDENTITY_CONFLICT",
                    message="管理员身份归属写入后无法读回，请重试",
                    status_code=409,
                    retryable=True,
                )
            return ResolvedUser(user=user, identity=loaded[1], created=True)

    def find_user_by_id(self, user_id: str) -> UserRecord | None:
        """按 UUID 读取用户。

        @param user_id: 用户 UUID 文本。
        @returns 用户记录；不存在时 ``None``。
        @raises ApplicationError: ``user_id`` 不是合法 UUID（400）。
        """
        _require_uuid(user_id, "user_id")
        return self._load_user(user_id)

    def find_identity(self, provider: LoginProviderId, subject: str) -> UserIdentityRecord | None:
        """按 ``(provider, subject)`` 读取身份。

        @param provider: 登录来源标识。
        @param subject: 该来源下已验证的主体标识。
        @returns 身份记录；不存在时 ``None``。
        """
        loaded = self._load_identity(provider, subject)
        return None if loaded is None else loaded[1]

    def set_user_status(
        self,
        user_id: str,
        status: UserStatus,
        *,
        now: datetime,
    ) -> UserRecord:
        """停用或恢复用户；``updated_at`` 只前进不回退。

        @param user_id: 用户 UUID 文本。
        @param status: 目标状态，``active`` 或 ``disabled``。
        @param now: 本次操作时间。
        @returns 更新后的用户记录。
        @raises ApplicationError: 用户不存在（404）；状态值非法或 id 格式非法（400）。
        """
        _require_uuid(user_id, "user_id")
        if status not in ("active", "disabled"):
            raise ApplicationError(
                code="INVALID_ARGUMENT",
                message="用户状态只能是 active 或 disabled",
                status_code=400,
                retryable=False,
            )
        with self.transaction():
            self._connection.execute(
                """
                UPDATE users
                SET status = ?,
                    updated_at = greatest(updated_at, CAST(? AS TIMESTAMPTZ))
                WHERE id = CAST(? AS UUID)
                """,
                [status, _iso(now), user_id],
            )
            updated = self._load_user(user_id)
            if updated is None:
                raise ApplicationError(
                    code="USER_NOT_FOUND",
                    message="用户不存在",
                    status_code=404,
                    retryable=False,
                )
        return updated

    def list_users(self, *, limit: int, offset: int) -> tuple[UserRecord, ...]:
        """按创建时间分页读取用户。

        排序键是 ``(created_at, id)``：只按 ``created_at`` 排序时，同一毫秒创建的多个
        用户在不同页之间顺序不稳定，会漏读或重复读。

        @param limit: 本页最大条数，必须在 ``1..MAX_USER_PAGE_SIZE`` 之间。
        @param offset: 跳过条数，必须非负。
        @returns 本页用户记录；越界时返回空元组。
        @raises ApplicationError: ``limit`` 或 ``offset`` 越界（400）。
        """
        if isinstance(limit, bool) or limit < 1 or limit > MAX_USER_PAGE_SIZE:
            raise ApplicationError(
                code="INVALID_ARGUMENT",
                message=f"limit 必须在 1 到 {MAX_USER_PAGE_SIZE} 之间",
                status_code=400,
                retryable=False,
            )
        if isinstance(offset, bool) or offset < 0:
            raise ApplicationError(
                code="INVALID_ARGUMENT",
                message="offset 不能为负数",
                status_code=400,
                retryable=False,
            )
        rows = self._connection.execute(
            f"""
            SELECT {_USER_COLUMNS} FROM users
            ORDER BY created_at, id
            LIMIT ? OFFSET ?
            """,
            [limit, offset],
        ).fetchall()
        return tuple(_user_from_row(row) for row in rows)

    def count_active_admins(self) -> int:
        """统计当前仍活动的管理员数量；「最后管理员保护」的判据。

        @returns ``role='admin'`` 且 ``status='active'`` 的用户数。
        """
        row = self._connection.execute(
            "SELECT count(*) FROM users WHERE role = 'admin' AND status = 'active'"
        ).fetchone()
        if row is None:
            raise RuntimeError("统计活动管理员未返回结果")
        return int(row[0])

    # ------------------------------------------------------------------
    # 内部读写原语
    # ------------------------------------------------------------------

    def _insert_user(
        self,
        *,
        user_id: str,
        display_name: str,
        status: str,
        role: str,
        stamp: str,
    ) -> None:
        """插入一行用户。"""
        self._connection.execute(
            """
            INSERT INTO users (id, display_name, status, role, created_at, updated_at)
            VALUES (CAST(? AS UUID), ?, ?, ?, CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ))
            """,
            [user_id, display_name, status, role, stamp, stamp],
        )

    def _insert_identity(
        self,
        *,
        user_id: str,
        identity: LoginIdentity,
        stamp: str,
    ) -> bool:
        """插入身份行；冲突时返回 ``False`` 而不是覆盖他人归属。

        ``RETURNING id`` 在冲突时返回**空结果集**、插入成功时返回一行，据此判断本次
        是否抢到了这条身份。这里刻意不用 ``DO UPDATE``：那会让后到的登录事务改写
        既有身份的归属。

        @returns 本次是否成功插入。
        """
        inserted = self._connection.execute(
            """
            INSERT INTO user_identities
                (id, user_id, provider, subject, email, display_name, created_at, updated_at)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), ?, ?, ?, ?,
                    CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ))
            ON CONFLICT (provider, subject) DO NOTHING
            RETURNING id
            """,
            [
                str(uuid4()), user_id, identity.provider, identity.subject,
                identity.email, identity.display_name, stamp, stamp,
            ],
        ).fetchone()
        return inserted is not None

    def _load_user(self, user_id: str) -> UserRecord | None:
        """按 UUID 读取用户行。"""
        row = self._connection.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE id = CAST(? AS UUID)",
            [user_id],
        ).fetchone()
        return None if row is None else _user_from_row(row)

    def _load_identity(
        self,
        provider: str,
        subject: str,
    ) -> tuple[UserRecord, UserIdentityRecord] | None:
        """按 ``(provider, subject)`` 连表读取身份与其用户。"""
        row = self._connection.execute(
            f"{_IDENTITY_SELECT} "
            "WHERE user_identities.provider = ? AND user_identities.subject = ?",
            [provider, subject],
        ).fetchone()
        if row is None:
            return None
        return _user_from_row(row[0:6]), _identity_from_row(row[6:14])


def _identity_exists_error(existing: UserRecord) -> ApplicationError:
    """构造「该身份已有用户」的明确失败。

    携带既有用户信息是刻意的：离线初始化需要告诉操作者身份被哪个用户占用，
    但绝不把它当作初始化成功返回。
    """
    return ApplicationError(
        code="USER_IDENTITY_EXISTS",
        message="该登录身份已存在用户，不能重复初始化",
        status_code=409,
        retryable=False,
        details={"userId": existing.id, "role": existing.role, "status": existing.status},
    )


def _require_uuid(value: str, field: str) -> None:
    """要求字段是合法 UUID 文本。

    @param value: 待校验的文本。
    @param field: 出错信息里的字段名。
    @raises ApplicationError: 不是合法 UUID（400）。
    """
    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError) as error:
        raise ApplicationError(
            code="INVALID_ARGUMENT",
            message=f"{field} 不是合法 UUID",
            status_code=400,
            retryable=False,
        ) from error


def _display_name_for(identity: LoginIdentity) -> str:
    """为新建用户挑一个非空显示名。

    ``users.display_name`` 有 ``trim > 0`` 约束，而 OAuth 不保证提供昵称（计划 §4.3
    明确「未提供邮箱不阻止注册」），因此按 昵称 → 邮箱 → subject 依次回退，绝不写空串。
    """
    for candidate in (identity.display_name, identity.email, identity.subject):
        if candidate is not None and candidate.strip():
            return candidate
    # subject 有 1..255 非空约束，走到这里说明上游没有做基本校验；宁可失败也不写空名。
    raise ApplicationError(
        code="INVALID_ARGUMENT",
        message="登录身份缺少可用的显示名称",
        status_code=400,
        retryable=False,
    )


def _user_from_row(row: tuple[object, ...]) -> UserRecord:
    """把查询行渲染成 :class:`UserRecord`，时间戳统一为 ISO 文本。"""
    return UserRecord(
        id=str(row[0]),
        display_name=str(row[1]),
        status=str(row[2]),  # type: ignore[arg-type] - 取值受 CHECK 约束限定为 active/disabled
        role=str(row[3]),  # type: ignore[arg-type] - 取值受 CHECK 约束限定为 admin/member
        created_at=_iso(row[4]),
        updated_at=_iso(row[5]),
    )


def _identity_from_row(row: tuple[object, ...]) -> UserIdentityRecord:
    """把查询行渲染成 :class:`UserIdentityRecord`，时间戳统一为 ISO 文本。"""
    return UserIdentityRecord(
        id=str(row[0]),
        user_id=str(row[1]),
        provider=str(row[2]),  # type: ignore[arg-type] - 取值受 CHECK 约束限定为已知来源
        subject=str(row[3]),
        email=None if row[4] is None else str(row[4]),
        display_name=None if row[5] is None else str(row[5]),
        created_at=_iso(row[6]),
        updated_at=_iso(row[7]),
    )


def _iso(value: object) -> str:
    """把 DuckDB 返回的时间值渲染为与 JavaScript ``toISOString()`` 同形的文本。

    DuckDB 读回 TIMESTAMPTZ 时带本地偏移（如 ``+08:00``），而写入时用的是 ``Z``；
    不归一化会让同一个用户在写入与读回时返回不同的字符串。
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return (
            moment.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return str(value)
