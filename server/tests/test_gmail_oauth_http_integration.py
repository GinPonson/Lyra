"""使用真实 factory 与 HTTPX MockTransport 验证 OAuth 传输合同。"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, parse_qsl, urlparse

import httpx
import pytest

from lyra.db.connection import open_lyra_database
from lyra.infrastructure.db.gmail_connection_repository import load_gmail_connection
from lyra.infrastructure.gmail.http_transport import (
    GoogleEndpointFailure,
    build_form_getter,
    build_json_getter,
    build_oauth_json_getter,
)
from lyra.infrastructure.gmail.oauth_transaction import GMAIL_READONLY_SCOPE
from lyra.infrastructure.gmail.service_factory import build_gmail_service
from lyra.security.secret_cipher import SecretCipher, master_key_from_hex

USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DEVICE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
IDENTITY_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
NOW = datetime(2026, 10, 18, 9, 0, 0, tzinfo=UTC)
REDIRECT_URI = "https://lyra.example.test/v1/auth/google/callback"
CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret"
REFRESH_TOKEN = "test-refresh-token"
ACCESS_TOKEN = "test-access-token"


def _active_database(tmp_path):
    """建立真实 factory 授权回调所需的活动用户和设备归属。"""
    database = open_lyra_database(str(tmp_path / "gmail-oauth.duckdb"))
    connection = database.connection
    connection.execute(
        "INSERT INTO users (id, display_name, status, role) "
        "VALUES (CAST(? AS UUID), 'OAuth owner', 'active', 'member')",
        [USER_ID],
    )
    connection.execute(
        """
        INSERT INTO user_identities (id, user_id, provider, subject)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), 'github', 'oauth-test-owner')
        """,
        [IDENTITY_ID, USER_ID],
    )
    connection.execute(
        """
        INSERT INTO paired_devices (
            id, user_id, identity_id, display_name, public_key_pem,
            public_key_fingerprint, key_algorithm, status, paired_at, updated_at
        ) VALUES (
            CAST(? AS UUID), CAST(? AS UUID), CAST(? AS UUID), 'OAuth test device',
            'test public key', ?, 'ed25519', 'active', CAST(? AS TIMESTAMPTZ),
            CAST(? AS TIMESTAMPTZ)
        )
        """,
        [DEVICE_ID, USER_ID, IDENTITY_ID, "d" * 64, NOW.isoformat(), NOW.isoformat()],
    )
    return database


def test_real_factory_runs_oauth_exchange_refresh_and_revoke_over_mocktransport(
    tmp_path, monkeypatch
) -> None:
    """授权、刷新和撤销均走真实依赖装配与 HTTPX 请求序列。"""
    database = _active_database(tmp_path)
    requests: list[tuple[str, str, dict[str, list[str]]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        form = dict(parse_qsl(request.content.decode("utf-8")))
        requests.append((request.method, parsed.path, {k: [v] for k, v in form.items()}))

        if parsed.path == "/token" and request.method == "POST":
            if form.get("grant_type") == "authorization_code":
                assert form["code"] == "test-authorization-code"
                assert form["code_verifier"]
                return httpx.Response(
                    200,
                    json={
                        "access_token": ACCESS_TOKEN,
                        "refresh_token": REFRESH_TOKEN,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": f"openid email {GMAIL_READONLY_SCOPE}",
                    },
                )
            if form.get("grant_type") == "refresh_token":
                assert form["refresh_token"] == REFRESH_TOKEN
                # Google refresh 响应可省略既有 refresh_token，也可省略未变化的 scope。
                return httpx.Response(
                    200,
                    json={
                        "access_token": "test-refreshed-access-token",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                )
        if parsed.path == "/v1/userinfo" and request.method == "GET":
            assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
            return httpx.Response(
                200,
                json={
                    "sub": "google-subject-1",
                    "email": "owner@example.test",
                    "email_verified": True,
                },
            )
        if parsed.path == "/revoke" and request.method == "POST":
            assert form == {"token": REFRESH_TOKEN}
            return httpx.Response(200)
        return httpx.Response(404, json={"error": "unexpected_endpoint"})

    mock_transport = httpx.MockTransport(respond)
    oauth_environment = {
        "LYRA_GOOGLE_CLIENT_ID": CLIENT_ID,
        "LYRA_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
        "LYRA_GOOGLE_REDIRECT_URI": REDIRECT_URI,
    }
    monkeypatch.setenv("LYRA_GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("LYRA_GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    service = build_gmail_service(
        connection=database.connection,
        environment=oauth_environment,
        cipher=SecretCipher(master_key_from_hex("0" * 64)),
        transport=mock_transport,
    )
    scoped = service.for_user(USER_ID)

    try:
        transaction = scoped.create_authorization_transaction(DEVICE_ID)
        state = parse_qs(urlparse(transaction["authorizationUrl"]).query)["state"][0]
        assert scoped.complete_authorization_callback(
            state=state,
            authorization_code="test-authorization-code",
            error=None,
        )
        result = scoped.get_authorization_transaction(
            authorization_id=transaction["authorizationId"], device_id=DEVICE_ID
        )
        assert result["status"] == "complete"
        connection_id = result["connection"]["id"]

        row = load_gmail_connection(database.connection, USER_ID, connection_id)
        assert row is not None and row.encrypted_refresh_token is not None
        sync_dependencies = service._deps.build_sync_dependencies(USER_ID)
        refreshed = sync_dependencies.refresh_access_token(row)
        assert refreshed == "test-refreshed-access-token"

        disconnected = scoped.disconnect(connection_id=connection_id, revoke=True)
        assert disconnected["status"] == "disconnected"
        persisted = load_gmail_connection(database.connection, USER_ID, connection_id)
        assert persisted is not None
        assert persisted.status == "disconnected"
        assert persisted.encrypted_refresh_token is None

        assert [(method, path) for method, path, _ in requests] == [
            ("POST", "/token"),
            ("GET", "/v1/userinfo"),
            ("POST", "/token"),
            ("POST", "/revoke"),
        ]
        assert requests[2][2]["grant_type"] == ["refresh_token"]
    finally:
        database.close()


def test_real_oauth_transport_hides_non_success_provider_body() -> None:
    """OAuth HTTP 非 2xx 只抛状态和稳定错误码，不泄漏供应商正文。"""
    provider_body = "private vendor details and token fragment"
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": provider_body},
        )
    )
    post_form = build_form_getter(transport=transport)

    with pytest.raises(GoogleEndpointFailure) as raised:
        post_form("https://oauth2.googleapis.com/token", {"code": "secret-code"}, 1)

    assert raised.value.status == 400
    assert raised.value.retryable is False
    assert raised.value.oauth_error_code == "invalid_grant"
    assert provider_body not in str(raised.value)
    assert raised.value.__cause__ is None


def test_real_oauth_transport_maps_network_errors_without_cause_details() -> None:
    """真实 HTTPX 网络异常折叠为可重试安全错误，不保留异常原文。"""
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private request details", request=request)

    post_form = build_form_getter(transport=httpx.MockTransport(fail))

    with pytest.raises(GoogleEndpointFailure) as raised:
        post_form("https://oauth2.googleapis.com/token", {"code": "secret-code"}, 1)

    assert raised.value.status is None
    assert raised.value.retryable is True
    assert "private request details" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_gmail_json_transport_keeps_its_status_payload_contract() -> None:
    """Gmail API getter 保持显式的 ``(status, payload)`` 合同。"""
    getter = build_json_getter(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"historyId": "123"})
        )
    )

    assert getter(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        {"authorization": "Bearer test-token"},
        1,
    ) == (200, {"historyId": "123"})


def test_oauth_userinfo_getter_maps_http_status_to_safe_exception() -> None:
    """OAuth userinfo getter不把 401 JSON 当身份字段处理。"""
    getter = build_oauth_json_getter(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                401, json={"error": "invalid_token", "error_description": "private"}
            )
        )
    )

    with pytest.raises(GoogleEndpointFailure) as raised:
        getter(
            "https://openidconnect.googleapis.com/v1/userinfo",
            {"authorization": "Bearer test-token"},
            1,
        )

    assert raised.value.status == 401
    assert raised.value.oauth_error_code == "invalid_token"
    assert "private" not in str(raised.value)
