"""Gmail 服务装配测试。

两处顺序即正确性：

1. **断开时先撤销、后落库**——顺序反过来会出现"本地已断开但 Google 侧仍授权"，
   那是最糟的组合；
2. **兑换后立刻从表里移除事务**——同一 state 不能兑换两次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.gmail_service import (
    AUTHORIZATION_TRANSACTION_TTL_MILLISECONDS,
    GmailOAuthConfig,
    GmailService,
)
from lyra.infrastructure.gmail.connection_credential import GMAIL_READONLY_SCOPE
from lyra.security.secret_cipher import SecretCipher

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
CONNECTION_ID = "11111111-1111-1111-1111-111111111111"
MAPPING_ID = "22222222-2222-2222-2222-222222222222"
DEVICE_ID = "device-1"
OAUTH = GmailOAuthConfig(
    client_id="client-1.apps.googleusercontent.com",
    redirect_uri="https://lyra.example.com/v1/auth/google/callback",
)


@dataclass
class _Deps:
    """外部动作桩。"""

    cipher: object = field(default_factory=lambda: SecretCipher(bytes(range(32))))
    now: object = lambda self=None: NOW
    new_id: object = lambda self=None: CONNECTION_ID
    connections: list = field(default_factory=list)
    mappings: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))

    def exchange_code(self, **kwargs):
        self._record("exchange_code", **kwargs)
        return {
            "identity": {"subject": "112233", "email": "owner@example.com"},
            "grant": {"refreshToken": "1//refresh", "grantedScopeText": GMAIL_READONLY_SCOPE},
        }

    def create_connection(self, connection):
        self._record("create_connection", id=connection.id)
        self.connections.append(SimpleNamespace(
            id=connection.id, google_subject=connection.google_subject,
            google_email=connection.google_email,
            encrypted_refresh_token=connection.encrypted_refresh_token,
            granted_scopes=connection.granted_scopes, history_id=connection.history_id,
            status=connection.status, last_error_code=connection.last_error_code,
            last_sync_at=connection.last_sync_at, created_at=connection.created_at,
            updated_at=connection.updated_at,
        ))

    def list_connections(self):
        return tuple(self.connections)

    def load_connection(self, connection_id):
        return next((c for c in self.connections if c.id == connection_id), None)

    def save_connection(self, record, *, expected_updated_at):
        self._record("save_connection", status=record.status)
        self.connections = [
            SimpleNamespace(
                id=record.id, google_subject=record.google_subject,
                google_email=record.google_email,
                encrypted_refresh_token=record.encrypted_refresh_token,
                granted_scopes=record.granted_scopes, history_id=record.history_id,
                status=record.status, last_error_code=record.last_error_code,
                last_sync_at=record.last_sync_at, created_at=record.created_at,
                updated_at=record.updated_at,
            )
            if c.id == record.id else c
            for c in self.connections
        ]

    def list_mappings(self, connection_id):
        return tuple(
            m for m in self.mappings.values() if m.connection_id == connection_id
        )

    def load_mapping(self, mapping_id):
        return self.mappings.get(mapping_id)

    def list_accounts(self):
        return ()

    def plan_mapping_update(self, **kwargs):
        raise AssertionError("测试未提供 plan_mapping_update")

    def update_mapping(self, **kwargs):
        self._record("update_mapping", **kwargs)

    def revoke(self, connection):
        self._record("revoke", id=connection.id)


def _service(deps=None, *, oauth=OAUTH) -> GmailService:
    """构造服务。"""
    return GmailService(dependencies=deps or _Deps(), oauth=oauth)


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


def test_unconfigured_service_refuses_completion() -> None:
    """OAuth 未配置时完成授权报 503。"""
    with pytest.raises(ApplicationError) as raised:
        _service(oauth=None).complete_authorization_transaction(
            device_id=DEVICE_ID, state="A" * 43, authorization_code="c"
        )

    assert raised.value.status_code == 503


# --------------------------------------------------------------------------
# 授权事务
# --------------------------------------------------------------------------


def test_transaction_returns_url_and_expiry_without_state() -> None:
    """**响应只含授权地址与过期时间，不含 state。**

    客户端从 Google 重定向里取 state，不需要从本响应里取。
    """
    transaction = _service().create_authorization_transaction(DEVICE_ID)

    assert transaction is not None
    assert set(transaction) == {"authorizationUrl", "expiresAt"}
    assert transaction["authorizationUrl"].startswith(
        "https://accounts.google.com/o/oauth2/v2/auth?"
    )
    assert transaction["expiresAt"] == "2026-10-12T09:10:00.000Z"


def test_transaction_ttl_is_ten_minutes() -> None:
    """事务有效期 10 分钟。"""
    assert AUTHORIZATION_TRANSACTION_TTL_MILLISECONDS == 10 * 60 * 1000


def test_completion_creates_a_connection() -> None:
    """完成授权建立连接并返回公开视图。"""
    deps = _Deps()
    service = _service(deps)
    service.create_authorization_transaction(DEVICE_ID)

    # 取出服务内部的事务 state（等价于 Google 回调携带的那个）。
    state = next(iter(service._transactions))
    result = service.complete_authorization_transaction(
        device_id=DEVICE_ID, state=state, authorization_code="code-1"
    )

    assert result["googleEmail"] == "owner@example.com"
    assert result["status"] == "pending_initial_scan"
    assert "encryptedRefreshToken" not in result
    assert any(name == "create_connection" for name, _ in deps.calls)


def test_completion_consumes_the_transaction() -> None:
    """**兑换后立刻移除事务**：同一 state 不能兑换两次。"""
    deps = _Deps()
    service = _service(deps)
    service.create_authorization_transaction(DEVICE_ID)
    state = next(iter(service._transactions))
    service.complete_authorization_transaction(
        device_id=DEVICE_ID, state=state, authorization_code="c"
    )

    assert state not in service._transactions


def test_completion_rejects_an_unknown_state() -> None:
    """未知 state 报无效。"""
    with pytest.raises(ApplicationError) as raised:
        _service().complete_authorization_transaction(
            device_id=DEVICE_ID, state="Z" * 43, authorization_code="c"
        )

    assert raised.value.code == "GOOGLE_AUTH_TRANSACTION_INVALID"


def test_transaction_is_bound_to_the_initiating_device() -> None:
    """**另一台设备拿到 state 也无法完成兑换。**"""
    service = _service()
    service.create_authorization_transaction(DEVICE_ID)
    state = next(iter(service._transactions))

    with pytest.raises(ApplicationError) as raised:
        service.complete_authorization_transaction(
            device_id="other-device", state=state, authorization_code="c"
        )

    assert raised.value.code == "GOOGLE_AUTH_TRANSACTION_INVALID"


# --------------------------------------------------------------------------
# 列出
# --------------------------------------------------------------------------


def test_lists_connections_and_mappings() -> None:
    """列出连接与映射。"""
    deps = _Deps()
    service = _service(deps)
    service.create_authorization_transaction(DEVICE_ID)
    state = next(iter(service._transactions))
    service.complete_authorization_transaction(
        device_id=DEVICE_ID, state=state, authorization_code="c"
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
    service.create_authorization_transaction(DEVICE_ID)
    state = next(iter(service._transactions))
    service.complete_authorization_transaction(
        device_id=DEVICE_ID, state=state, authorization_code="c"
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
