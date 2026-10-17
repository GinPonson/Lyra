"""Gmail 服务：把各部件接到路由所需的方法上。

编排 OAuth 授权事务、连接生命周期、账户映射与（通过 ``GmailSyncService``）同步。

**授权事务存在进程内存里**：它是一次性短时状态（TTL 10 分钟），不该进数据库——
持久化它等于把"未完成的授权"变成需要清理的历史数据，而进程重启后它本就应当消失。

**授权事务绑定发起设备**：另一台设备拿到 state 也无法完成兑换。

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

from dataclasses import dataclass
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
    consume_google_oauth_transaction,
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
        transactions: dict[str, Any] | None = None,
    ) -> None:
        self._deps = dependencies
        self._oauth = oauth
        # 键为 state 的一次性事务表；进程重启即清空（本就应该如此）。
        #
        # **由 :meth:`for_user` 的副本共享**：创建事务的请求与完成事务的请求是两个
        # 请求，各自拿到自己的服务副本；表若不共享，完成那一步永远找不到事务。
        self._transactions: dict[str, Any] = {} if transactions is None else transactions

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
        )

    # ------------------------------------------------------------------
    # 授权事务
    # ------------------------------------------------------------------

    def create_authorization_transaction(self, device_id: str) -> dict[str, Any] | None:
        """创建一次授权事务。

        **只返回 ``authorizationUrl`` 与 ``expiresAt``，不返回 ``state``**：
        客户端从 Google 重定向里取 state，不需要（也不该）从本响应里取。

        **不额外绑定用户**：事务已绑定发起设备，而设备归属唯一，因此设备匹配已经
        蕴含用户匹配——再多存一个 user_id 只是同一约束的第二份副本。

        @param device_id: 发起授权的设备。
        @returns 授权地址与过期时间；OAuth 未配置时为 ``None``。
        """
        if self._oauth is None:
            return None
        transaction = create_google_oauth_transaction(
            device_id, self._deps.now(), AUTHORIZATION_TRANSACTION_TTL_MILLISECONDS
        )
        self._transactions[transaction.state] = transaction.record
        request = build_google_authorization_request(
            transaction, client_id=self._oauth.client_id,
            redirect_uri=self._oauth.redirect_uri,
        )
        return {
            "authorizationUrl": request.authorization_url,
            "expiresAt": request.expires_at,
        }

    def complete_authorization_transaction(
        self, *, device_id: str, state: str, authorization_code: str
    ) -> dict[str, Any]:
        """用授权码完成授权并建立**属于当前用户**的连接。

        @param device_id: 发起授权的设备。
        @param state: 回调携带的 state。
        @param authorization_code: Google 授权码。
        @returns 公开的连接视图。
        @raises ApplicationError: OAuth 未配置（503）、事务无效/过期/已用（400），
            或该用户已连接同一个 Google 账号（409）。
        """
        if self._oauth is None:
            raise ApplicationError(
                code="GMAIL_CONNECTION_ERROR", message="Google OAuth 客户端尚未配置",
                status_code=503, retryable=False,
            )
        record = self._transactions.get(state)
        if record is None:
            raise ApplicationError(
                code="GOOGLE_AUTH_TRANSACTION_INVALID",
                message="Google 授权事务无效", status_code=400, retryable=False,
            )
        try:
            consumed = consume_google_oauth_transaction(
                record, state=state, device_id=device_id, now=self._deps.now()
            )
        except GmailOAuthError as error:
            raise ApplicationError(
                code=error.code, message=error.message,
                status_code=400, retryable=False,
            ) from error
        # **消费后立即移除**：同一 state 不能兑换两次。留在表里靠 consumed_at 拦截
        # 也行，但删掉更彻底——表也不会随时间无限增长。
        self._transactions.pop(state, None)

        exchanged = self._deps.exchange_code(
            authorization_code=authorization_code,
            code_verifier=consumed.code_verifier,
            client_id=self._oauth.client_id,
            redirect_uri=self._oauth.redirect_uri,
            client_secret=self._oauth.client_secret,
        )
        connection = create_gmail_connection_record(
            connection_id=self._deps.new_id(),
            identity=_identity(exchanged["identity"]),
            grant=_grant(exchanged["grant"]),
            cipher=self._deps.cipher,
            now=self._deps.now(),
        )
        # 归属由依赖视图注入：``create_connection`` 落库时带上当前用户的 user_id。
        #
        # 同一 Google 账号在**同一用户**下已有连接时插入会冲突（409）；在**另一个
        # 用户**下则是各自独立的一条连接，不再互相顶掉（唯一键 ``(user_id,
        # google_subject)``，P0 契约 §3.12）。
        self._deps.create_connection(connection)
        return create_public_gmail_connection(connection).to_json()

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
        explicit_account_id: str | None,
    ) -> dict[str, Any]:
        """执行一次属于当前用户的增量同步或有界重扫。

        **用户活动状态在真正的执行点再查一次**：请求进入时 ``require_user`` 已经拒绝
        过停用用户（入队检查），但同步是长流程，用户可能在拉取期间被停用。依赖视图里
        的每一次读取/写入都会重新确认活动状态，因此停用一旦提交，本次同步既不会继续
        拉取，也不会入账（计划 §4.4；P0 契约 §3.5 说明本服务没有独立后台 worker，
        "排队任务"即等待单写队列或已在进行中的请求）。

        @param connection_id: 连接 ID；必须属于当前用户。
        @param confirmed_rescan_after_date: 用户确认的重扫起始日期。
        @param explicit_account_id: 用户显式指定的账户。
        @returns 同步摘要。
        @raises ApplicationError: 连接不存在（404）、用户已停用（403）、
            OAuth 未配置（503）、需要确认重扫（409）、授权失效（401）等。
        """
        return sync_gmail_connection(
            connection_id=connection_id,
            confirmed_rescan_after_date=confirmed_rescan_after_date,
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
