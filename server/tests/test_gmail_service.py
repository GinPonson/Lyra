"""Gmail 服务装配测试。

两处顺序即正确性：

1. **断开时先撤销、后落库**——顺序反过来会出现"本地已断开但 Google 侧仍授权"，
   那是最糟的组合；
2. **兑换后立刻从表里移除事务**——同一 state 不能兑换两次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.gmail_service import (
    AUTHORIZATION_TRANSACTION_TTL_MILLISECONDS,
    GmailOAuthConfig,
    GmailService,
)
from lyra.infrastructure.gmail.connection_credential import GMAIL_READONLY_SCOPE
from lyra.infrastructure.gmail.google_oauth_client import GoogleEndpointFailure
from lyra.security.secret_cipher import SecretCipher

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
CONNECTION_ID = "11111111-1111-1111-1111-111111111111"
MAPPING_ID = "22222222-2222-2222-2222-222222222222"
DEVICE_ID = "device-1"
USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OAUTH = GmailOAuthConfig(
    client_id="client-1.apps.googleusercontent.com",
    redirect_uri="https://lyra.example.com/v1/auth/google/callback",
)


@dataclass
class _Deps:
    """外部动作桩。"""

    cipher: object = field(default_factory=lambda: SecretCipher(bytes(range(32))))
    connections: list = field(default_factory=list)
    mappings: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    now_value: datetime = NOW
    exchange_error: Exception | None = None
    scope_text: str = GMAIL_READONLY_SCOPE
    authorization_error: ApplicationError | None = None
    revoke_during_exchange: bool = False
    revoke_before_upsert: bool = False

    def now(self):
        return self.now_value

    def new_id(self):
        return CONNECTION_ID

    def assert_authorization_active(self, user_id, device_id):
        self._record(
            "assert_authorization_active", user_id=user_id, device_id=device_id
        )
        if self.authorization_error is not None:
            raise self.authorization_error

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))

    def exchange_code(self, **kwargs):
        self._record("exchange_code", **kwargs)
        if self.exchange_error is not None:
            raise self.exchange_error
        if self.revoke_during_exchange:
            self.authorization_error = ApplicationError(
                code="DEVICE_CREDENTIAL_INVALID", message="设备已撤销",
                status_code=401, retryable=False,
            )
        return {
            "identity": {"subject": "112233", "email": "owner@example.com"},
            "grant": {"refreshToken": "1//refresh", "grantedScopeText": self.scope_text},
        }

    def load_connection_by_subject(self, user_id, google_subject):
        return next((
            c for c in self.connections
            if c.user_id == user_id and c.google_subject == google_subject
        ), None)

    def upsert_connection(self, user_id, connection, *, device_id):
        self._record("upsert_connection", user_id=user_id, id=connection.id)
        if self.revoke_before_upsert:
            self.authorization_error = ApplicationError(
                code="DEVICE_CREDENTIAL_INVALID", message="设备已撤销",
                status_code=401, retryable=False,
            )
        self.assert_authorization_active(user_id, device_id)
        existing = self.load_connection_by_subject(user_id, connection.google_subject)
        row = SimpleNamespace(
            user_id=user_id,
            id=connection.id, google_subject=connection.google_subject,
            google_email=connection.google_email,
            encrypted_refresh_token=connection.encrypted_refresh_token,
            granted_scopes=connection.granted_scopes, history_id=connection.history_id,
            status=connection.status, last_error_code=connection.last_error_code,
            last_sync_at=connection.last_sync_at, created_at=connection.created_at,
            updated_at=connection.updated_at,
        )
        if existing is not None:
            row.id = existing.id
            row.history_id = existing.history_id
            row.last_sync_at = existing.last_sync_at
            row.created_at = existing.created_at
            self.connections.remove(existing)
        self.connections.append(row)
        return row

    def list_connections(self, user_id):
        return tuple(c for c in self.connections if c.user_id == user_id)

    def load_connection(self, user_id, connection_id):
        return next((
            c for c in self.connections if c.user_id == user_id and c.id == connection_id
        ), None)

    def save_connection(self, user_id, record, *, expected_updated_at):
        self._record("save_connection", status=record.status)
        self.connections = [
            SimpleNamespace(
                user_id=user_id,
                id=record.id, google_subject=record.google_subject,
                google_email=record.google_email,
                encrypted_refresh_token=record.encrypted_refresh_token,
                granted_scopes=record.granted_scopes, history_id=record.history_id,
                status=record.status, last_error_code=record.last_error_code,
                last_sync_at=record.last_sync_at, created_at=record.created_at,
                updated_at=record.updated_at,
            )
            if c.user_id == user_id and c.id == record.id else c
            for c in self.connections
        ]

    def list_mappings(self, user_id, connection_id):
        _ = user_id
        return tuple(
            m for m in self.mappings.values() if m.connection_id == connection_id
        )

    def load_mapping(self, user_id, mapping_id):
        _ = user_id
        return self.mappings.get(mapping_id)

    def list_accounts(self, user_id):
        _ = user_id
        return ()

    def plan_mapping_update(self, **kwargs):
        raise AssertionError("测试未提供 plan_mapping_update")

    def update_mapping(self, user_id, **kwargs):
        _ = user_id
        self._record("update_mapping", **kwargs)

    def revoke(self, connection):
        self._record("revoke", id=connection.id)


def _service(deps=None, *, oauth=OAUTH) -> GmailService:
    """构造绑定测试用户的服务。"""
    return GmailService(dependencies=deps or _Deps(), oauth=oauth).for_user(USER_ID)


def _start(service: GmailService, device_id: str = DEVICE_ID) -> tuple[dict, str]:
    """发起授权并从只发给 Google 的地址取回测试用 state。"""
    transaction = service.create_authorization_transaction(device_id)
    assert transaction is not None
    state = parse_qs(urlparse(transaction["authorizationUrl"]).query)["state"][0]
    return transaction, state


def _mapping(mapping_id=MAPPING_ID, *, connection_id=CONNECTION_ID, suffix="276"):
    """构造映射行。"""
    return SimpleNamespace(
        id=mapping_id, connection_id=connection_id, account_suffix=suffix,
        account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        created_at="2026-10-12T09:00:00.000Z", updated_at="2026-10-12T09:00:00.000Z",
    )


# --------------------------------------------------------------------------
# OAuth 未配置
# --------------------------------------------------------------------------


def test_unconfigured_service_returns_none_for_transactions() -> None:
    """OAuth 未配置时返回 ``None``（路由据此给 503）。"""
    assert _service(oauth=None).create_authorization_transaction(DEVICE_ID) is None
    assert _service(oauth=None).configured is False


def test_unconfigured_service_does_not_claim_callback() -> None:
    """未配置 Gmail OAuth 时由共享回调继续原 Google 登录流程。"""
    assert not _service(oauth=None).complete_authorization_callback(
        state="A" * 43, authorization_code="c", error=None
    )


# --------------------------------------------------------------------------
# 授权事务
# --------------------------------------------------------------------------


def test_transaction_returns_url_and_expiry_without_state() -> None:
    """**响应只含授权地址与过期时间，不含 state。**

    客户端从 Google 重定向里取 state，不需要从本响应里取。
    """
    transaction, _ = _start(_service())

    assert transaction is not None
    assert set(transaction) == {
        "authorizationId", "authorizationUrl", "expiresAt", "pollIntervalSeconds"
    }
    assert transaction["pollIntervalSeconds"] == 1
    assert transaction["authorizationUrl"].startswith(
        "https://accounts.google.com/o/oauth2/v2/auth?"
    )
    assert transaction["expiresAt"] == "2026-10-12T09:10:00.000Z"


def test_transaction_ttl_is_ten_minutes() -> None:
    """事务有效期 10 分钟。"""
    assert AUTHORIZATION_TRANSACTION_TTL_MILLISECONDS == 10 * 60 * 1000


def test_callback_creates_a_connection_and_poll_result_is_repeatable() -> None:
    """浏览器回调只落库；原设备可在 TTL 内重复读取安全连接结果。"""
    deps = _Deps()
    service = _service(deps)
    transaction, state = _start(service)

    assert service.complete_authorization_callback(
        state=state, authorization_code="code-1", error=None
    )
    first = service.get_authorization_transaction(
        authorization_id=transaction["authorizationId"], device_id=DEVICE_ID
    )
    second = service.get_authorization_transaction(
        authorization_id=transaction["authorizationId"], device_id=DEVICE_ID
    )

    assert first == second
    assert first["status"] == "complete"
    assert first["connection"]["googleEmail"] == "owner@example.com"
    assert first["connection"]["status"] == "pending_initial_scan"
    assert "encryptedRefreshToken" not in str(first)
    assert any(name == "upsert_connection" for name, _ in deps.calls)
    assert any(name == "assert_authorization_active" for name, _ in deps.calls)


def test_callback_state_is_one_time_consumed() -> None:
    """成功回调后 state 不能再次兑换，且不会重复写入连接。"""
    deps = _Deps()
    service = _service(deps)
    _, state = _start(service)
    assert service.complete_authorization_callback(
        state=state, authorization_code="c", error=None
    )
    calls_before_replay = len([name for name, _ in deps.calls if name == "exchange_code"])

    assert service.complete_authorization_callback(
        state=state, authorization_code="replayed", error=None
    )
    assert len([name for name, _ in deps.calls if name == "exchange_code"]) == calls_before_replay


def test_unknown_callback_state_falls_through_to_google_login() -> None:
    """未知 state 由共享回调继续原 Google 登录流程。"""
    assert not _service().complete_authorization_callback(
        state="Z" * 43, authorization_code="c", error=None
    )


def test_transaction_poll_is_bound_to_user_and_device() -> None:
    """轮询只允许创建授权事务的用户和设备读取。"""
    root = GmailService(dependencies=_Deps(), oauth=OAUTH)
    owner = root.for_user(USER_ID)
    other_user = root.for_user(OTHER_USER_ID)
    transaction, _ = _start(owner)

    for wrong_service, device_id in ((other_user, DEVICE_ID), (owner, "other-device")):
        with pytest.raises(ApplicationError) as raised:
            wrong_service.get_authorization_transaction(
                authorization_id=transaction["authorizationId"], device_id=device_id
            )
        assert raised.value.code == "GOOGLE_AUTH_TRANSACTION_INVALID"


def test_expired_callback_is_recorded_and_poll_returns_expired() -> None:
    """10 分钟边界后 callback 不兑换，并向原设备报告过期。"""
    deps = _Deps()
    service = _service(deps)
    transaction, state = _start(service)
    deps.now_value = NOW + timedelta(minutes=10)

    assert service.complete_authorization_callback(
        state=state, authorization_code="late-code", error=None
    )
    with pytest.raises(ApplicationError) as raised:
        service.get_authorization_transaction(
            authorization_id=transaction["authorizationId"], device_id=DEVICE_ID
        )

    assert raised.value.code == "GOOGLE_AUTH_TRANSACTION_EXPIRED"
    assert not any(name == "exchange_code" for name, _ in deps.calls)


def test_user_denial_is_a_safe_poll_failure() -> None:
    """用户拒绝 Gmail 权限时只记录稳定错误，浏览器不会收到 OAuth 细节。"""
    service = _service()
    transaction, state = _start(service)

    assert service.complete_authorization_callback(
        state=state, authorization_code=None, error="access_denied"
    )
    with pytest.raises(ApplicationError) as raised:
        service.get_authorization_transaction(
            authorization_id=transaction["authorizationId"], device_id=DEVICE_ID
        )

    assert raised.value.code == "GMAIL_AUTHORIZATION_DENIED"
    assert raised.value.status_code == 400


def test_network_exchange_failure_is_recorded_without_upstream_details() -> None:
    """Google 网络失败进入可重试的安全轮询错误。"""
    deps = _Deps(exchange_error=GoogleEndpointFailure(None, True))
    service = _service(deps)
    transaction, state = _start(service)

    assert service.complete_authorization_callback(
        state=state, authorization_code="code", error=None
    )
    with pytest.raises(ApplicationError) as raised:
        service.get_authorization_transaction(
            authorization_id=transaction["authorizationId"], device_id=DEVICE_ID
        )

    assert raised.value.code == "GMAIL_CONNECTION_ERROR"
    assert raised.value.status_code == 503
    assert "Google endpoint failed" not in raised.value.message


def test_missing_gmail_scope_is_recorded_as_a_safe_failure() -> None:
    """只给登录 scope 时不保存连接，并向 App 返回明确权限错误。"""
    deps = _Deps(scope_text="openid email")
    service = _service(deps)
    transaction, state = _start(service)

    assert service.complete_authorization_callback(
        state=state, authorization_code="code", error=None
    )
    with pytest.raises(ApplicationError) as raised:
        service.get_authorization_transaction(
            authorization_id=transaction["authorizationId"], device_id=DEVICE_ID
        )

    assert raised.value.code == "GMAIL_SCOPE_MISSING"
    assert raised.value.status_code == 403
    assert deps.connections == []


@pytest.mark.parametrize(
    ("code", "status_code"),
    [("DEVICE_CREDENTIAL_INVALID", 401), ("USER_DISABLED", 403)],
)
def test_late_callback_does_not_restore_a_revoked_device_or_disabled_user(
    code: str, status_code: int
) -> None:
    """授权等待期间设备撤销或用户停用后，迟到回调不能建立/恢复连接。"""
    deps = _Deps(authorization_error=ApplicationError(
        code=code, message="当前设备或用户已不可用", status_code=status_code,
        retryable=False,
    ))
    service = _service(deps)
    transaction, state = _start(service)

    assert service.complete_authorization_callback(
        state=state, authorization_code="late-code", error=None
    )
    with pytest.raises(ApplicationError) as raised:
        service.get_authorization_transaction(
            authorization_id=transaction["authorizationId"], device_id=DEVICE_ID
        )

    assert raised.value.code == code
    assert raised.value.status_code == status_code
    assert not any(name == "exchange_code" for name, _ in deps.calls)
    assert deps.connections == []


@pytest.mark.parametrize("revocation_point", ["during_exchange", "before_upsert"])
def test_callback_rechecks_authorization_after_google_network_wait(
    revocation_point: str,
) -> None:
    """设备在 Google 网络往返或最终写入前撤销时，不允许保存新凭据。"""
    deps = _Deps(
        revoke_during_exchange=revocation_point == "during_exchange",
        revoke_before_upsert=revocation_point == "before_upsert",
    )
    service = _service(deps)
    transaction, state = _start(service)

    assert service.complete_authorization_callback(
        state=state, authorization_code="code", error=None
    )
    with pytest.raises(ApplicationError) as raised:
        service.get_authorization_transaction(
            authorization_id=transaction["authorizationId"], device_id=DEVICE_ID
        )

    assert raised.value.code == "DEVICE_CREDENTIAL_INVALID"
    assert deps.connections == []


def test_reauthorization_preserves_connection_identity_and_sync_history() -> None:
    """重授权更新凭据但保留连接 ID、游标、映射与已完成连接状态。"""
    deps = _Deps()
    deps.connections.append(SimpleNamespace(
        user_id=USER_ID,
        id=CONNECTION_ID,
        google_subject="112233",
        google_email="old@example.com",
        encrypted_refresh_token="old-ciphertext",
        granted_scopes=GMAIL_READONLY_SCOPE,
        history_id="history-42",
        status="disconnected",
        last_error_code=None,
        last_sync_at="2026-10-11T09:00:00.000Z",
        created_at="2026-09-01T09:00:00.000Z",
        updated_at="2026-10-11T09:00:00.000Z",
    ))
    service = _service(deps)
    transaction, state = _start(service)

    assert service.complete_authorization_callback(
        state=state, authorization_code="new-code", error=None
    )
    result = service.get_authorization_transaction(
        authorization_id=transaction["authorizationId"], device_id=DEVICE_ID
    )
    connection = deps.connections[0]

    assert result["status"] == "complete"
    assert connection.id == CONNECTION_ID
    assert connection.google_email == "owner@example.com"
    assert connection.encrypted_refresh_token != "old-ciphertext"
    assert connection.history_id == "history-42"
    assert connection.last_sync_at == "2026-10-11T09:00:00.000Z"
    assert connection.created_at == "2026-09-01T09:00:00.000Z"
    assert connection.status == "needs_resync"
    assert result["connection"]["status"] == "needs_resync"


# --------------------------------------------------------------------------
# 列出
# --------------------------------------------------------------------------


def test_lists_connections_and_mappings() -> None:
    """列出连接与映射。"""
    deps = _Deps()
    service = _service(deps)
    _, state = _start(service)
    service.complete_authorization_callback(
        state=state, authorization_code="c", error=None
    )
    deps.mappings[MAPPING_ID] = _mapping()

    assert len(service.list_connections()) == 1
    assert service.list_mappings(CONNECTION_ID)[0]["accountSuffix"] == "276"


def test_listing_mappings_for_another_connection_is_empty() -> None:
    """别的连接读不到本连接的映射。"""
    deps = _Deps()
    deps.mappings[MAPPING_ID] = _mapping()
    assert _service(deps).list_mappings("99999999-9999-9999-9999-999999999999") == []


# --------------------------------------------------------------------------
# 改绑
# --------------------------------------------------------------------------


def test_unknown_mapping_is_a_404() -> None:
    """映射不存在 → 404。"""
    with pytest.raises(ApplicationError) as raised:
        _service().update_mapping(
            connection_id=CONNECTION_ID, mapping_id=MAPPING_ID,
            target_account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )

    assert raised.value.status_code == 404


def test_unchanged_mapping_skips_the_write() -> None:
    """目标账户未变时不写库——避免制造一次无意义的 ``updated_at`` 变更。"""
    deps = _Deps()
    deps.mappings[MAPPING_ID] = _mapping()
    deps.plan_mapping_update = lambda **kwargs: SimpleNamespace(
        changed=False, expected_account_id="a", next_mapping=_mapping()
    )
    service = _service(deps)

    result = service.update_mapping(
        connection_id=CONNECTION_ID, mapping_id=MAPPING_ID,
        target_account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    assert result["id"] == MAPPING_ID
    assert not any(name == "update_mapping" for name, _ in deps.calls)


# --------------------------------------------------------------------------
# 断开
# --------------------------------------------------------------------------


def _connected_service():
    """建立一个已连接的服务的桩。"""
    deps = _Deps()
    service = _service(deps)
    _, state = _start(service)
    service.complete_authorization_callback(
        state=state, authorization_code="c", error=None
    )
    return service, deps


def test_disconnect_without_revoke_does_not_call_google() -> None:
    """不撤销时不请求 Google。"""
    service, deps = _connected_service()

    result = service.disconnect(connection_id=CONNECTION_ID, revoke=False)

    assert result["status"] == "disconnected"
    assert not any(name == "revoke" for name, _ in deps.calls)


def test_disconnect_with_revoke_calls_google_first() -> None:
    """**先撤销、后落库。**

    顺序反过来会出现"本地已断开但 Google 侧仍授权"——最糟的组合。
    """
    service, deps = _connected_service()

    service.disconnect(connection_id=CONNECTION_ID, revoke=True)

    names = [name for name, _ in deps.calls]
    assert names.index("revoke") < names.index("save_connection")


def test_revocation_failure_leaves_the_connection_intact() -> None:
    """**撤销失败时不落库**：连接保持在可用状态，用户可重试。"""
    service, deps = _connected_service()

    def failing(connection):
        raise RuntimeError("network down")

    deps.revoke = failing
    deps.calls.clear()

    with pytest.raises(RuntimeError):
        service.disconnect(connection_id=CONNECTION_ID, revoke=True)

    assert not any(name == "save_connection" for name, _ in deps.calls)
    assert service.list_connections()[0]["status"] != "disconnected"


def test_unknown_connection_disconnect_is_a_404() -> None:
    """连接不存在 → 404。"""
    with pytest.raises(ApplicationError) as raised:
        _service().disconnect(connection_id=CONNECTION_ID, revoke=False)

    assert raised.value.status_code == 404


def test_disconnect_is_idempotent() -> None:
    """重复断开不改写时间（§98.4）。"""
    service, _ = _connected_service()
    first = service.disconnect(connection_id=CONNECTION_ID, revoke=False)
    second = service.disconnect(connection_id=CONNECTION_ID, revoke=False)

    assert first["updatedAt"] == second["updatedAt"]
