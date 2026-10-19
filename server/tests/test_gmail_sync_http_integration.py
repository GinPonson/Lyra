"""真实 factory 的 Gmail API tuple 传输与同步贯通回归。"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from urllib.parse import parse_qs, parse_qsl, urlparse

import httpx

from lyra.db.connection import open_lyra_database
from lyra.infrastructure.db.gmail_connection_repository import (
    create_gmail_account_mapping_row,
    create_gmail_connection_row,
    load_gmail_connection,
)
from lyra.infrastructure.gmail.connection_credential import (
    GMAIL_READONLY_SCOPE,
    GoogleOfflineGrant,
    VerifiedGoogleIdentity,
    create_gmail_connection_record,
)
from lyra.infrastructure.gmail.service_factory import build_gmail_service
from lyra.security.secret_cipher import SecretCipher, master_key_from_hex

USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONNECTION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ACCOUNT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
NOW = datetime(2026, 10, 18, 9, 0, 0, tzinfo=UTC)
CLIENT_ID = "mock-gmail-client.apps.googleusercontent.com"
CLIENT_SECRET = "mock-gmail-client-secret"
REFRESH_TOKEN = "mock-gmail-refresh-token"
ACCESS_TOKEN = "mock-gmail-access-token"


def _encoded(text: str) -> str:
    """把合成邮件正文编码为 Gmail 的无填充 base64url。"""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _body(*, symbol: str = "SYNTH", trade_date: str = "1/5/2026") -> str:
    """构造结构有效、数值均为虚构的单笔 eConfirm 正文。"""
    return (
        "Account ending in 276\n\n"
        f"Symbol:\n\n{symbol}\n\n"
        "Security Description:\n\nSYNTHETIC CORP\n\n"
        "Action:\n\nPurchase\n\n"
        "Type:\n\nMargin\n\n"
        f"Trade Date:\n\n{trade_date}\n\n"
        "Total Amount\n\n2\n\n$20.00\n\n$40.00\n\n$40.00\n"
    )


def _full_message(message_id: str, *, symbol: str, trade_date: str) -> dict:
    """生成标准 Gmail ``format=full`` MIME 结构。"""
    return {
        "id": message_id,
        "internalDate": "1790000000000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "Schwab <donotreply@mail.schwab.com>"},
                {"name": "Subject", "value": "Schwab eConfirms"},
                {"name": "Date", "value": "Mon, 5 Jan 2026 10:00:00 +0000"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "headers": [{
                        "name": "Content-Type",
                        "value": "text/plain; charset=UTF-8",
                    }],
                    "body": {"data": _encoded(_body(symbol=symbol, trade_date=trade_date))},
                }
            ],
        },
    }


def _seed_connection(database) -> None:
    """创建一个待首次同步的活动连接和明确的账户尾号映射。"""
    connection = database.connection
    connection.execute(
        "INSERT INTO users (id, display_name, status, role) "
        "VALUES (CAST(? AS UUID), 'Gmail sync owner', 'active', 'member')",
        [USER_ID],
    )
    connection.execute(
        """
        INSERT INTO accounts (
            id, user_id, display_name, legacy_account_key, masked_suffix, status
        ) VALUES (
            CAST(? AS UUID), CAST(? AS UUID), 'Synthetic brokerage',
            'Individual_XXX276', '276', 'active'
        )
        """,
        [ACCOUNT_ID, USER_ID],
    )
    cipher = SecretCipher(master_key_from_hex("0" * 64))
    record = create_gmail_connection_record(
        connection_id=CONNECTION_ID,
        identity=VerifiedGoogleIdentity(
            subject="synthetic-google-subject",
            email="owner@example.test",
            email_verified=True,
        ),
        grant=GoogleOfflineGrant(
            refresh_token=REFRESH_TOKEN,
            granted_scope_text=GMAIL_READONLY_SCOPE,
        ),
        cipher=cipher,
        now=NOW,
    )
    create_gmail_connection_row(connection, USER_ID, record)
    create_gmail_account_mapping_row(
        connection, USER_ID,
        connection_id=CONNECTION_ID,
        account_suffix="276",
        account_id=ACCOUNT_ID,
        at=NOW,
    )


def test_factory_syncs_paged_full_messages_then_nonempty_history(tmp_path, monkeypatch) -> None:
    """真实 factory 经 tuple HTTP 边界完成两页入账与 history 增量去重。"""
    database = open_lyra_database(str(tmp_path / "gmail-sync-http.duckdb"))
    _seed_connection(database)
    monkeypatch.setenv("LYRA_GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("LYRA_GOOGLE_CLIENT_SECRET", CLIENT_SECRET)

    requests: list[tuple[str, str, dict[str, list[str]]]] = []
    full_messages = {
        "m1": _full_message("m1", symbol="SYNTH", trade_date="1/5/2026"),
        # 第二页是另一个合成证券，验证分页结果都会实际拉取并入账。
        "m2": _full_message("m2", symbol="SECOND", trade_date="1/5/2026"),
        "m3": _full_message("m3", symbol="NEXT", trade_date="1/6/2026"),
    }

    def respond(request: httpx.Request) -> httpx.Response:
        """按 Google OAuth 与 Gmail API 路由返回虚构响应，不记录认证头。"""
        parsed = urlparse(str(request.url))
        query = parse_qs(parsed.query)
        form = dict(parse_qsl(request.content.decode("utf-8"))) if request.content else {}
        requests.append((request.method, parsed.path, query))

        if parsed.path == "/token" and request.method == "POST":
            assert form["grant_type"] == "refresh_token"
            assert form["refresh_token"] == REFRESH_TOKEN
            return httpx.Response(
                200,
                json={
                    "access_token": ACCESS_TOKEN,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )

        assert request.headers.get("authorization") == f"Bearer {ACCESS_TOKEN}"
        if parsed.path.endswith("/users/me/profile"):
            return httpx.Response(
                200,
                json={
                    "emailAddress": "owner@example.test",
                    "historyId": "100",
                    "messagesTotal": 3,
                    "threadsTotal": 3,
                },
            )
        if parsed.path.endswith("/users/me/messages") and "pageToken" not in query:
            return httpx.Response(
                200,
                json={"messages": [{"id": "m1"}], "nextPageToken": "page-2"},
            )
        if parsed.path.endswith("/users/me/messages") and query.get("pageToken") == ["page-2"]:
            return httpx.Response(200, json={"messages": [{"id": "m2"}]})
        if parsed.path.endswith("/users/me/history"):
            return httpx.Response(
                200,
                json={
                    "historyId": "120",
                    "history": [{
                        "messagesAdded": [
                            # 在初扫窗口中同时出现的邮件可能也落在 profile 基线之后；
                            # 它用于验证重复 Gmail message 的稳定跳过。
                            {"message": {"id": "m1"}},
                            {"message": {"id": "m3"}},
                        ]
                    }],
                },
            )

        prefix = "/gmail/v1/users/me/messages/"
        if parsed.path.startswith(prefix):
            message_id = parsed.path.removeprefix(prefix)
            if query.get("format") == ["metadata"]:
                return httpx.Response(
                    200,
                    json={
                        "id": message_id,
                        "payload": {"headers": [
                            {"name": "From", "value": "Schwab <donotreply@mail.schwab.com>"},
                            {"name": "Subject", "value": "Schwab eConfirms"},
                        ]},
                    },
                )
            if query.get("format") == ["full"]:
                return httpx.Response(200, json=full_messages[message_id])
        return httpx.Response(404, json={"error": "synthetic unexpected request"})

    oauth_environment = {
        "LYRA_GOOGLE_CLIENT_ID": CLIENT_ID,
        "LYRA_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
        "LYRA_GOOGLE_REDIRECT_URI": "https://lyra.example.test/v1/auth/google/callback",
    }
    service = build_gmail_service(
        connection=database.connection,
        environment=oauth_environment,
        cipher=SecretCipher(master_key_from_hex("0" * 64)),
        transport=httpx.MockTransport(respond),
    ).for_user(USER_ID)

    try:
        initial = service.sync(
            connection_id=CONNECTION_ID,
            confirmed_rescan_after_date=None,
            scan_all=True,
            explicit_account_id=None,
        )
        assert initial["mode"] == "bounded_rescan"
        assert initial["truncated"] is False
        assert initial["insertedTransactions"] == 2
        assert initial["conflictMessages"] == 0
        assert initial["connection"]["historyId"] == "100"

        incremental = service.sync(
            connection_id=CONNECTION_ID,
            confirmed_rescan_after_date=None,
            explicit_account_id=None,
        )
        assert incremental["mode"] == "incremental"
        assert incremental["alreadyImportedMessages"] == 1
        assert incremental["insertedTransactions"] == 1
        assert incremental["connection"]["historyId"] == "120"

        persisted = load_gmail_connection(database.connection, USER_ID, CONNECTION_ID)
        assert persisted is not None
        assert persisted.status == "active"
        assert persisted.history_id == "120"
        assert database.connection.execute(
            "SELECT count(*) FROM gmail_messages WHERE connection_id = CAST(? AS UUID)",
            [CONNECTION_ID],
        ).fetchone()[0] == 3
        assert database.connection.execute(
            "SELECT count(*) FROM transactions WHERE account_id = CAST(? AS UUID)",
            [ACCOUNT_ID],
        ).fetchone()[0] == 3

        message_pages = [
            query for method, path, query in requests
            if method == "GET" and path.endswith("/users/me/messages")
        ]
        assert len(message_pages) == 2
        assert message_pages[0]["q"]
        assert "pageToken" not in message_pages[0]
        assert message_pages[1]["pageToken"] == ["page-2"]
        assert any(path.endswith("/users/me/history") for _, path, _ in requests)
        assert sum(
            query.get("format") == ["full"] for _, path, query in requests
            if "/users/me/messages/" in path
        ) == 4
    finally:
        database.close()
