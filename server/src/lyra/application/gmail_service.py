"""Gmail 服务：把各部件接到路由所需的方法上。

编排 OAuth 授权事务、连接生命周期、账户映射与（通过 ``GmailSyncService``）同步。

**授权事务存在进程内存里**：它是一次性短时状态（TTL 10 分钟），不该进数据库——
持久化它等于把"未完成的授权"变成需要清理的历史数据，而进程重启后它本就应当消失。

**授权事务绑定发起设备**：另一台设备拿到 state 也无法完成兑换。
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
    """

    def __init__(self, *, dependencies: Any, oauth: GmailOAuthConfig | None) -> None:
        self._deps = dependencies
        self._oauth = oauth
        # 键为 state 的一次性事务表；进程重启即清空（本就应该如此）。
        self._transactions: dict[str, Any] = {}

    @property
    def configured(self) -> bool:
        """OAuth 是否已配置。"""
        return self._oauth is not None

    # ------------------------------------------------------------------
    # 授权事务
    # ------------------------------------------------------------------

    def create_authorization_transaction(self, device_id: str) -> dict[str, Any] | None:
        """创建一次授权事务。

        **只返回 ``authorizationUrl`` 与 ``expiresAt``，不返回 ``state``**：
        客户端从 Google 重定向里取 state，不需要（也不该）从本响应里取。

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
        """用授权码完成授权并建立连接。

        @param device_id: 发起授权的设备。
        @param state: 回调携带的 state。
        @param authorization_code: Google 授权码。
        @returns 公开的连接视图。
        @raises ApplicationError: OAuth 未配置（503）或事务无效/过期/已用。
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
        try:
            self._deps.create_connection(connection)
        except ApplicationError:
            # 同一 Google 账号已有连接：**不新建**，否则两条连接的同步游标会互相覆盖。
            raise
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
        @raises ApplicationError: 连接不存在（404）或撤销失败（503，可重试）。
        """
        connection = self._deps.load_connection(connection_id)
        if connection is None:
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
