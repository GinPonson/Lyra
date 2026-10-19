"""Gmail 服务：把各部件接到路由所需的方法上。

编排 OAuth 授权事务、连接生命周期、账户映射与（通过 ``GmailSyncService``）同步。

**授权事务存在进程内存里**：它是一次性短时状态（TTL 10 分钟），不该进数据库；
服务重启后待完成的授权自然失效。

**授权事务绑定用户与发起设备**：回调只取服务端保存的归属，只有原用户和设备可轮询。

**用户范围通过 :meth:`GmailService.for_user` 注入，而不是每个方法的参数**：路由持有
的是一个单例服务（``app.state.gmail_service``），而「当前用户」是**每请求**才确定的。
``for_user`` 返回一个把 ``user_id`` 绑进依赖视图的服务副本，因此：

- 每一条仓储调用都天然带用户范围，不存在"某个方法漏传 user_id 退化成全库"的路径；
- 业务方法的形状（``list_connections()`` / ``disconnect(connection_id=...)``）保持不变，
  路由与既有单元测试只多一层 ``for_user``，不必逐个方法改签名。

**没有"不带用户的默认视图"**：``for_user`` 需要显式 ``user_id``；未绑定用户的服务在
真实依赖上调用任何方法都会因缺少参数而**立刻报错**，而不是退回全库语义。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from threading import RLock
from typing import Any

from ..domain.gmail.sync_plan import GmailSyncError
from ..infrastructure.gmail.connection_credential import (
    GmailConnectionRecord,
    create_gmail_connection_record,
    disconnect_gmail_connection,
)
from ..infrastructure.gmail.google_oauth_client import (
    GoogleEndpointFailure,
    build_google_authorization_request,
)
from ..infrastructure.gmail.oauth_transaction import (
    GmailOAuthError,
    GoogleOAuthTransactionStore,
    create_google_oauth_transaction,
)
from .app_error import ApplicationError
from .gmail_sync_service import sync_gmail_connection
from .gmail_views import (
    create_public_gmail_account_mapping_list,
    create_public_gmail_connection,
)

# 授权事务有效期：10 分钟。用户完成一次 Google 授权通常远快于此。
AUTHORIZATION_TRANSACTION_TTL_MILLISECONDS = 10 * 60 * 1000


@dataclass(frozen=True)
class GmailOAuthConfig:
    """Google OAuth 客户端配置。"""

    client_id: str
    redirect_uri: str
    client_secret: str | None = None


class GmailService:
    """Gmail 连接与授权服务。

    @param dependencies: 外部动作集合。
    @param oauth: OAuth 配置；``None`` 表示未配置。
    @param transactions: 一次性授权事务表；``for_user`` 的副本共享同一个表。
    """

    def __init__(
        self,
        *,
        dependencies: Any,
        oauth: GmailOAuthConfig | None,
        transactions: GoogleOAuthTransactionStore | None = None,
        user_id: str | None = None,
        connection_lock: Any = None,
    ) -> None:
        self._deps = dependencies
        self._oauth = oauth
        # 键为 state 的一次性事务表；进程重启即清空（本就应该如此）。
        #
        # **由 :meth:`for_user` 的副本共享**：创建事务的请求与完成事务的请求是两个
        # 请求，各自拿到自己的服务副本；表若不共享，完成那一步永远找不到事务。
        self._transactions = (
            GoogleOAuthTransactionStore() if transactions is None else transactions
        )
        self._user_id = user_id
        # 同一服务进程内串行化重授权的「读已有 ID → 加密凭据 → upsert」步骤，
        # 确保密文 AAD 使用保留的连接 ID。
        self._connection_lock = RLock() if connection_lock is None else connection_lock

    @property
    def configured(self) -> bool:
        """OAuth 是否已配置。"""
        return self._oauth is not None

    def for_user(self, user_id: str) -> GmailService:
        """返回绑定到指定用户的服务视图。

        **这是路由唯一的取服务方式**：用户来自 ``request.state.auth.user_id``（由
        ``require_user`` 从访问令牌解析），绝不来自请求正文或查询串（计划 §4.2）。

        @param user_id: 当前用户的 UUID 文本；**必填**。
        @returns 所有仓储调用都限定在该用户内的服务副本。
        """
        return GmailService(
            dependencies=_UserScopedGmailDependencies(self._deps, user_id),
            oauth=self._oauth,
            transactions=self._transactions,
            user_id=user_id,
            connection_lock=self._connection_lock,
        )

    # ------------------------------------------------------------------
    # 授权事务
    # ------------------------------------------------------------------

    def create_authorization_transaction(self, device_id: str) -> dict[str, Any] | None:
        """创建一次授权事务。

        浏览器将 state 直接回传服务端，App 只持有授权地址和独立轮询 ID；
        App 不接收 Google 回调中的授权码，也不复制 state 来完成授权。

        @param device_id: 发起授权的设备。
        @returns authorizationId、授权地址、过期时间与轮询间隔；OAuth 未配置时为 ``None``。
        """
        if self._oauth is None:
            return None
        if self._user_id is None:
            raise ApplicationError(
                code="DEVICE_CREDENTIAL_INVALID", message="设备访问令牌无效",
                status_code=401, retryable=False,
            )
        now = self._deps.now()
        authorization_id = str(uuid.uuid4())
        transaction = create_google_oauth_transaction(
            device_id, now, AUTHORIZATION_TRANSACTION_TTL_MILLISECONDS,
            user_id=self._user_id, authorization_id=authorization_id,
        )
        request = build_google_authorization_request(
            transaction, client_id=self._oauth.client_id,
            redirect_uri=self._oauth.redirect_uri,
        )
        try:
            self._transactions.create(transaction.record, now=now)
        except GmailOAuthError as error:
            raise ApplicationError(
                code=error.code, message=error.message,
                status_code=503, retryable=error.retryable,
            ) from error
        return {
            "authorizationId": authorization_id,
            "authorizationUrl": request.authorization_url,
            "expiresAt": request.expires_at,
            "pollIntervalSeconds": 1,
        }

    def complete_authorization_callback(
        self, *, state: str, authorization_code: str | None, error: str | None
    ) -> bool:
        """处理 Google 浏览器回调并把安全结果留给原设备轮询。

        回调不接受用户或设备标识；归属从短期服务端事务读取。未知 state 返回 ``False``，
        让共享回调路由继续尝试原有 Google 身份登录流程。

        @param state: Google 回调携带的 state 原文。
        @param authorization_code: 成功时 Google 返回的授权码。
        @param error: 用户拒绝等 Google 错误标识。
        @returns 是否识别为 Gmail 事务；无论结果如何都不把凭据返回浏览器。
        @raises ApplicationError: OAuth 未配置或连接持久化失败。
        """
        if self._oauth is None:
            return False
        try:
            record = self._transactions.claim(state=state, now=self._deps.now())
        except GmailOAuthError as transaction_error:
            if transaction_error.code == "GOOGLE_AUTH_TRANSACTION_INVALID":
                return False
            # 事务已过期或回调已重放时，不把 state 交给登录回调继续处理。
            return True
        if error is not None:
            denied = error == "access_denied"
            self._transactions.fail(
                state_hash=record.state_hash,
                code="GMAIL_AUTHORIZATION_DENIED" if denied else "GMAIL_CONNECTION_ERROR",
                message=(
                    "用户取消了 Gmail 授权"
                    if denied else "Google 暂时无法完成 Gmail 授权，请稍后重试"
                ),
                status_code=400 if denied else 503,
                retryable=not denied,
            )
            return True
        if (
            not isinstance(authorization_code, str)
            or not 1 <= len(authorization_code.strip()) <= 4_096
        ):
            self._transactions.fail(
                state_hash=record.state_hash,
                code="GOOGLE_TOKEN_EXCHANGE_FAILED",
                message="Google 授权响应无效，请重新连接 Gmail",
                status_code=400,
                retryable=False,
            )
            return True
        if record.user_id == "":
            # 创建事务时即验证用户绑定；保留此检查防止错误的内存记录被兑换。
            raise ApplicationError(
                code="GOOGLE_AUTH_TRANSACTION_INVALID",
                message="Google 授权事务无效", status_code=400, retryable=False,
            )
        try:
            # 浏览器回调没有请求认证上下文，事务中的用户与设备是唯一可信归属。
            # 已绑定视图（例如单元测试或内部调用）无需再包一层用户适配器。
            scoped = (
                self
                if self._user_id == record.user_id
                else self.for_user(record.user_id)
            )
            scoped._deps.assert_authorization_active(
                record.user_id, record.device_id
            )
            exchanged = scoped._deps.exchange_code(
                authorization_code=authorization_code.strip(),
                code_verifier=record.code_verifier,
                client_id=self._oauth.client_id,
                redirect_uri=self._oauth.redirect_uri,
                client_secret=self._oauth.client_secret,
            )
            identity = _identity(exchanged["identity"])
            grant = _grant(exchanged["grant"])
            # Google 网络请求可能持续十几秒；若用户在等待期间停用账户或撤销设备，
            # 不应再继续创建连接或恢复旧凭据。
            scoped._deps.assert_authorization_active(
                record.user_id, record.device_id
            )
            with self._connection_lock:
                existing = scoped._deps.load_connection_by_subject(identity.subject)
                connection_id = scoped._deps.new_id() if existing is None else existing.id
                connection = create_gmail_connection_record(
                    connection_id=connection_id,
                    identity=identity,
                    grant=grant,
                    cipher=scoped._deps.cipher,
                    now=scoped._deps.now(),
                )
                if existing is not None:
                    # 原连接已有同步游标或映射时保留它们；重新授权后要求用户明确选定
                    # 扫描范围，避免凭新 token 静默扩大历史扫描范围。
                    status = (
                        "needs_resync"
                        if existing.status in ("disconnected", "error", "needs_resync")
                        else existing.status
                    )
                    connection = _preserve_connection_history(
                        connection, existing, status=status
                    )
                persisted = scoped._deps.upsert_connection(
                    connection, device_id=record.device_id
                )
            public_connection = create_public_gmail_connection(persisted).to_json()
            self._transactions.complete(
                state_hash=record.state_hash, connection=public_connection
            )
        except GmailOAuthError as oauth_error:
            status_code = _oauth_http_status(oauth_error)
            self._transactions.fail(
                state_hash=record.state_hash,
                code=oauth_error.code,
                message=oauth_error.message,
                status_code=status_code,
                retryable=oauth_error.retryable,
            )
        except GoogleEndpointFailure as endpoint_error:
            self._transactions.fail(
                state_hash=record.state_hash,
                code="GMAIL_CONNECTION_ERROR",
                message="无法连接 Google，请稍后重试",
                status_code=503,
                retryable=endpoint_error.retryable,
            )
        except ApplicationError as application_error:
            self._transactions.fail(
                state_hash=record.state_hash,
                code=application_error.code,
                message=application_error.message,
                status_code=application_error.status_code,
                retryable=application_error.retryable,
            )
        return True

    def get_authorization_transaction(
        self, *, authorization_id: str, device_id: str
    ) -> dict[str, Any]:
        """读取原设备 Gmail 授权的结果，直到事务 TTL 到期。

        @param authorization_id: 创建响应中的轮询 ID。
        @param device_id: 当前认证设备 ID。
        @returns pending 状态或带公开连接视图的 complete 状态。
        @raises ApplicationError: 用户/设备不匹配、事务失效或授权失败。
        """
        if self._user_id is None:
            raise ApplicationError(
                code="DEVICE_CREDENTIAL_INVALID", message="设备访问令牌无效",
                status_code=401, retryable=False,
            )
        try:
            record = self._transactions.get(
                authorization_id=authorization_id,
                user_id=self._user_id,
                device_id=device_id,
                now=self._deps.now(),
            )
        except GmailOAuthError as error:
            raise ApplicationError(
                code=error.code, message=error.message,
                status_code=410 if error.code == "GOOGLE_AUTH_TRANSACTION_EXPIRED" else 404,
                retryable=error.retryable,
            ) from error
        if record.status in ("pending", "processing"):
            return {"status": "pending"}
        if record.status == "failed":
            raise ApplicationError(
                code=record.failure_code or "GMAIL_CONNECTION_ERROR",
                message=record.failure_message or "无法完成 Gmail 授权",
                status_code=record.failure_status_code or 503,
                retryable=record.failure_retryable,
            )
        if record.result is None:
            raise ApplicationError(
                code="GMAIL_CONNECTION_ERROR", message="Gmail 授权结果不可用",
                status_code=503, retryable=False,
            )
        return {"status": "complete", "connection": record.result}

    # ------------------------------------------------------------------
    # 连接与映射
    # ------------------------------------------------------------------

    def list_connections(self) -> list[dict[str, Any]]:
        """列出全部连接。"""
        return [
            create_public_gmail_connection(row).to_json()
            for row in self._deps.list_connections()
        ]

    def list_mappings(self, connection_id: str) -> list[dict[str, Any]]:
        """列出某连接的账户映射。"""
        rows = self._deps.list_mappings(connection_id)
        return [
            view.to_json()
            for view in create_public_gmail_account_mapping_list(connection_id, rows)
        ]

    def update_mapping(
        self, *, connection_id: str, mapping_id: str, target_account_id: str
    ) -> dict[str, Any]:
        """改绑一条账户映射。

        @returns 公开的映射视图。
        @raises ApplicationError: 映射不存在（404）、不属于该连接（400）
            或目标账户不合法（409）。
        """
        mapping = self._deps.load_mapping(mapping_id)
        if mapping is None:
            raise ApplicationError(
                code="GMAIL_CONNECTION_ERROR", message="Gmail 映射不存在",
                status_code=404, retryable=False,
            )
        plan = self._deps.plan_mapping_update(
            connection_id=connection_id, mapping=mapping,
            accounts=self._deps.list_accounts(), target_account_id=target_account_id,
        )
        if plan.changed:
            self._deps.update_mapping(
                mapping_id=mapping_id,
                expected_account_id=plan.expected_account_id,
                next_account_id=plan.next_mapping.account_id,
                at=self._deps.now(),
            )
        updated = self._deps.load_mapping(mapping_id)
        return create_public_gmail_account_mapping_list(
            connection_id, (updated,)
        )[0].to_json()

    def disconnect(self, *, connection_id: str, revoke: bool) -> dict[str, Any]:
        """断开连接。

        ``revoke=True`` 时**先向 Google 撤销授权，成功后才落库**。顺序反过来会出现
        "本地已断开但 Google 侧仍授权"的状态——那是最糟的组合。

        @returns 公开的连接视图。
        @raises ApplicationError: 连接不存在或不属于当前用户（404）、撤销失败（503，可重试）。
        """
        connection = self._deps.load_connection(connection_id)
        if connection is None:
            # 不属于该用户的连接与不存在的连接返回同一个 404，不区分二者。
            raise ApplicationError(
                code="GMAIL_CONNECTION_ERROR", message="Gmail 连接不存在",
                status_code=404, retryable=False,
            )
        if revoke:
            try:
                self._deps.revoke(connection)
            except (GmailSyncError, GoogleEndpointFailure) as error:
                raise ApplicationError(
                    code="GMAIL_CONNECTION_ERROR",
                    message="Google 未确认 Gmail 授权撤销，请稍后重试",
                    status_code=503, retryable=True,
                ) from error
        record = _to_record(connection)
        disconnected = disconnect_gmail_connection(record, self._deps.now())
        self._deps.save_connection(disconnected, expected_updated_at=connection.updated_at)
        return create_public_gmail_connection(disconnected).to_json()

    # ------------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------------

    def sync(
        self,
        *,
        connection_id: str,
        confirmed_rescan_after_date: str | None,
        scan_all: bool = False,
        explicit_account_id: str | None,
    ) -> dict[str, Any]:
        """执行一次属于当前用户的增量同步或有界扫描。

        **用户活动状态在真正的执行点再查一次**：请求进入时 ``require_user`` 已经拒绝
        过停用用户（入队检查），但同步是长流程，用户可能在拉取期间被停用。依赖视图里
        的每一次读取/写入都会重新确认活动状态，因此停用一旦提交，本次同步既不会继续
        拉取，也不会入账（计划 §4.4；P0 契约 §3.5 说明本服务没有独立后台 worker，
        "排队任务"即等待单写队列或已在进行中的请求）。

        @param connection_id: 连接 ID；必须属于当前用户。
        @param confirmed_rescan_after_date: 用户确认的重扫起始日期。
        @param scan_all: 用户明确选择不限制首次扫描或重扫日期。
        @param explicit_account_id: 用户显式指定的账户。
        @returns 同步摘要。
        @raises ApplicationError: 连接不存在（404）、用户已停用（403）、
            OAuth 未配置（503）、需要确认重扫（409）、授权失效（401）等。
        """
        return sync_gmail_connection(
            connection_id=connection_id,
            confirmed_rescan_after_date=confirmed_rescan_after_date,
            scan_all=scan_all,
            explicit_account_id=explicit_account_id,
            dependencies=self._deps.build_sync_dependencies(),
        )


class _UserScopedGmailDependencies:
    """把 ``user_id`` 绑进每一次依赖调用的适配器。

    **为什么不在服务方法上加 ``user_id`` 参数**：那样每个调用点都要记住传参，漏传
    一次就退化成全库语义，而且失败是静默的（读到别人的数据）。把用户放进依赖视图后，
    仓储调用**不可能**漏掉用户范围——它根本不在调用点上。

    这里刻意只转发 :class:`GmailService` 真正会用到的方法，不做通用 ``__getattr__``
    代理：依赖接口扩张时应当显式暴露，而不是自动获得一个可能不带用户范围的新方法。

    @param inner: 真实的依赖集合；其方法首参为 ``user_id``。
    @param user_id: 绑定的用户 UUID 文本。
    """

    def __init__(self, inner: Any, user_id: str) -> None:
        self._inner = inner
        self._user_id = user_id

    @property
    def user_id(self) -> str:
        """绑定的用户 UUID 文本；测试与日志据此确认作用域。"""
        return self._user_id

    @property
    def cipher(self) -> Any:
        """凭据加密器；授权码交换后创建连接记录时使用。"""
        return self._inner.cipher

    def now(self) -> Any:
        """服务端可信时间。"""
        return self._inner.now()

    def new_id(self) -> str:
        """新连接 ID。"""
        return self._inner.new_id()

    def assert_authorization_active(self, user_id: str, device_id: str) -> None:
        """确认等待 OAuth 的用户和设备在回调时仍有效且仍保持归属。"""
        if user_id != self._user_id:
            raise ApplicationError(
                code="GOOGLE_AUTH_TRANSACTION_INVALID",
                message="Google 授权事务无效",
                status_code=404,
                retryable=False,
            )
        self._inner.assert_authorization_active(self._user_id, device_id)

    def exchange_code(self, **kwargs: Any) -> dict:
        """用授权码换取凭据与身份。"""
        return self._inner.exchange_code(**kwargs)

    def create_connection(self, record: Any) -> None:
        """插入连接行，归属为绑定的用户。"""
        self._inner.create_connection(self._user_id, record)

    def list_connections(self) -> tuple:
        """列出绑定用户的连接。"""
        return self._inner.list_connections(self._user_id)

    def load_connection(self, connection_id: str) -> Any:
        """读取一条属于绑定用户的连接。"""
        return self._inner.load_connection(self._user_id, connection_id)

    def load_connection_by_subject(self, google_subject: str) -> Any:
        """按 Google subject 读取绑定用户的既有连接。"""
        return self._inner.load_connection_by_subject(self._user_id, google_subject)

    def upsert_connection(self, record: Any, *, device_id: str | None = None) -> Any:
        """插入或更新绑定用户的连接，并在事务内校验原授权设备仍有效。"""
        return self._inner.upsert_connection(
            self._user_id, record, device_id=device_id
        )

    def save_connection(
        self, record: Any, *, expected_updated_at: str, **overrides: Any
    ) -> None:
        """保存绑定用户的连接状态（带 CAS）。"""
        self._inner.save_connection(
            self._user_id, record, expected_updated_at=expected_updated_at, **overrides
        )

    def list_mappings(self, connection_id: str) -> tuple:
        """列出绑定用户某连接的映射。"""
        return self._inner.list_mappings(self._user_id, connection_id)

    def load_mapping(self, mapping_id: str) -> Any:
        """读取一条属于绑定用户的映射。"""
        return self._inner.load_mapping(self._user_id, mapping_id)

    def list_accounts(self) -> tuple:
        """列出绑定用户的账本账户。"""
        return self._inner.list_accounts(self._user_id)

    def plan_mapping_update(self, **kwargs: Any) -> Any:
        """规划一次映射改绑；账户集合已由绑定用户范围产出。"""
        return self._inner.plan_mapping_update(**kwargs)

    def update_mapping(self, **kwargs: Any) -> None:
        """执行改绑（旧账户 ID 做 CAS），并确认目标账户属于绑定用户。"""
        self._inner.update_mapping(self._user_id, **kwargs)

    def revoke(self, connection: Any) -> None:
        """向 Google 撤销授权。"""
        self._inner.revoke(connection)

    def build_sync_dependencies(self) -> Any:
        """构造绑定用户的同步依赖集合。"""
        return self._inner.build_sync_dependencies(self._user_id)


def _identity(payload: dict[str, Any]) -> Any:
    """把交换结果里的身份转成领域对象。"""
    from ..infrastructure.gmail.connection_credential import VerifiedGoogleIdentity

    return VerifiedGoogleIdentity(
        subject=payload["subject"], email=payload["email"],
        email_verified=payload.get("emailVerified", True),
    )


def _grant(payload: dict[str, Any]) -> Any:
    """把交换结果里的授权转成领域对象。"""
    from ..infrastructure.gmail.connection_credential import GoogleOfflineGrant

    return GoogleOfflineGrant(
        refresh_token=payload["refreshToken"],
        granted_scope_text=payload["grantedScopeText"],
    )


def _to_record(row: Any) -> GmailConnectionRecord:
    """把持久化行转回领域记录。"""
    return GmailConnectionRecord(
        id=row.id, google_subject=row.google_subject, google_email=row.google_email,
        encrypted_refresh_token=row.encrypted_refresh_token,
        granted_scopes=row.granted_scopes, history_id=row.history_id,
        status=row.status, last_error_code=row.last_error_code,
        last_sync_at=row.last_sync_at, created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _preserve_connection_history(
    fresh: GmailConnectionRecord, existing: Any, *, status: str
) -> GmailConnectionRecord:
    """用新凭据覆盖连接，同时保留原连接历史和创建时间。

    @param fresh: 使用原连接 ID 加密的新授权记录。
    @param existing: 当前用户同 subject 的持久化连接。
    @param status: 新授权后的同步状态。
    @returns 保留游标、最近同步时间和创建时间的记录。
    """
    from dataclasses import replace

    return replace(
        fresh,
        history_id=existing.history_id,
        status=status,
        last_error_code=None,
        last_sync_at=existing.last_sync_at,
        created_at=existing.created_at,
    )


def _oauth_http_status(error: GmailOAuthError) -> int:
    """把 OAuth 校验错误映射到安全的轮询状态码。"""
    if error.code in ("GMAIL_SCOPE_MISSING", "GOOGLE_IDENTITY_UNVERIFIED"):
        return 403
    if error.retryable:
        return 503
    return 400


def load_gmail_google_oauth_config(environment: Any) -> GmailOAuthConfig | None:
    """读取 Google OAuth 配置。

    **clientId 与 redirectUri 缺一即视为未配置**（返回 ``None``）：只有一半配置时
    授权地址是拼不出来的，让服务在启动时就报"未配置"比在用户点击时失败好。

    密钥是可选的：桌面与本地客户端（PKCE）不需要它。

    @param environment: 环境变量映射。
    @returns 配置；缺失必需项时为 ``None``。
    """
    client_id = (environment.get("LYRA_GOOGLE_CLIENT_ID") or "").strip()
    redirect_uri = (environment.get("LYRA_GOOGLE_REDIRECT_URI") or "").strip()
    if client_id == "" or redirect_uri == "":
        return None
    client_secret = (environment.get("LYRA_GOOGLE_CLIENT_SECRET") or "").strip()
    return GmailOAuthConfig(
        client_id=client_id,
        redirect_uri=redirect_uri,
        client_secret=client_secret or None,
    )
