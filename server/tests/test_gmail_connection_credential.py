"""Gmail 连接凭据测试。

两处安全性质：

1. **refresh token 的密文绑定连接 ID 作为 AAD**——换一个连接 ID 解密就失败，
   密文**无法在连接之间搬运**；
2. **必须包含 Gmail 只读作用域**，且作用域文本被**规范化（去重 + 排序）**后存储，
   否则同一组权限会因顺序不同而产生不同记录。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lyra.infrastructure.gmail.connection_credential import (
    GMAIL_READONLY_SCOPE,
    GmailOAuthError,
    GoogleOfflineGrant,
    VerifiedGoogleIdentity,
    assert_gmail_readonly_scope,
    create_gmail_connection_record,
    disconnect_gmail_connection,
)
from lyra.security.secret_cipher import SecretCipher

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
CIPHER = SecretCipher(bytes(range(32)))


def _identity(**overrides) -> VerifiedGoogleIdentity:
    base = {"subject": "112233", "email": "Owner@Example.com", "email_verified": True}
    base.update(overrides)
    return VerifiedGoogleIdentity(**base)  # type: ignore[arg-type]


def _grant(**overrides) -> GoogleOfflineGrant:
    base = {"refresh_token": "1//refresh-secret", "granted_scope_text": GMAIL_READONLY_SCOPE}
    base.update(overrides)
    return GoogleOfflineGrant(**base)  # type: ignore[arg-type]


def _record(connection_id: str = "conn-1"):
    return create_gmail_connection_record(
        connection_id=connection_id, identity=_identity(), grant=_grant(),
        cipher=CIPHER, now=NOW,
    )


# --------------------------------------------------------------------------
# 作用域
# --------------------------------------------------------------------------


def test_accepts_the_readonly_scope() -> None:
    """只读作用域通过。"""
    assert assert_gmail_readonly_scope(GMAIL_READONLY_SCOPE) == (GMAIL_READONLY_SCOPE,)


def test_scopes_are_deduplicated_and_sorted() -> None:
    """**去重并排序**：同一组权限不应因顺序或重复而产生不同记录。"""
    text = "https://b.example/scope  https://a.example/scope https://b.example/scope"

    scopes = assert_gmail_readonly_scope(f"{text} {GMAIL_READONLY_SCOPE}")

    assert scopes == (
        "https://a.example/scope", "https://b.example/scope", GMAIL_READONLY_SCOPE
    )


def test_missing_readonly_scope_is_rejected() -> None:
    """**缺少只读作用域必须拒绝**——少了它连接根本不能用。"""
    with pytest.raises(GmailOAuthError) as raised:
        assert_gmail_readonly_scope("https://www.googleapis.com/auth/userinfo.email")

    assert raised.value.code == "GMAIL_SCOPE_MISSING"


def test_empty_scope_text_is_rejected() -> None:
    """空作用域文本同样报缺少。"""
    with pytest.raises(GmailOAuthError):
        assert_gmail_readonly_scope("   ")


# --------------------------------------------------------------------------
# 创建
# --------------------------------------------------------------------------


def test_creates_a_pending_connection() -> None:
    """新连接状态为 pending_initial_scan，且没有游标。"""
    record = _record()

    assert record.status == "pending_initial_scan"
    assert record.history_id is None
    assert record.last_sync_at is None
    assert record.last_error_code is None
    assert record.created_at == "2026-10-12T09:00:00.000Z"


def test_email_is_lowercased() -> None:
    """邮箱统一小写，避免大小写造成身份差异。"""
    assert _record().google_email == "owner@example.com"


def test_refresh_token_is_encrypted_and_bound_to_the_connection() -> None:
    """**密文绑定连接 ID 作为 AAD**：换一个连接 ID 解密就失败。

    这样一次数据库写入错误不会让 A 连接的凭据被 B 连接使用。
    """
    record = _record("conn-1")
    assert record.encrypted_refresh_token is not None
    assert "1//refresh-secret" not in record.encrypted_refresh_token

    # 用正确的连接 ID 能解出原文。
    assert CIPHER.decrypt(
        record.encrypted_refresh_token, "gmail-connection:conn-1"
    ) == "1//refresh-secret"

    # 换成另一个连接 ID 就解不开——**密文无法搬运**。
    with pytest.raises(Exception):
        CIPHER.decrypt(record.encrypted_refresh_token, "gmail-connection:conn-2")


def test_two_connections_of_the_same_account_have_different_ciphertext() -> None:
    """同一账号的两条连接密文不同（AAD 不同 + IV 随机）。"""
    first = _record("conn-1")
    second = _record("conn-2")

    assert first.encrypted_refresh_token != second.encrypted_refresh_token


@pytest.mark.parametrize(
    "overrides",
    [
        {"email_verified": False},
        {"email": "not-an-email"},
        {"email": "  "},
        {"subject": "  "},
    ],
)
def test_rejects_unverified_or_malformed_identity(overrides: dict) -> None:
    """**未验证的邮箱不能作为身份依据**——任何人都能把别人的邮箱填进自己的资料。"""
    with pytest.raises(ValueError):
        create_gmail_connection_record(
            connection_id="conn-1", identity=_identity(**overrides), grant=_grant(),
            cipher=CIPHER, now=NOW,
        )


def test_rejects_empty_refresh_token() -> None:
    """refresh token 不能为空。"""
    with pytest.raises(ValueError, match="refresh token"):
        create_gmail_connection_record(
            connection_id="conn-1", identity=_identity(),
            grant=_grant(refresh_token="   "), cipher=CIPHER, now=NOW,
        )


def test_rejects_naive_time() -> None:
    """时间必须带时区。"""
    with pytest.raises(ValueError, match="必须带时区"):
        create_gmail_connection_record(
            connection_id="conn-1", identity=_identity(), grant=_grant(),
            cipher=CIPHER, now=datetime(2026, 10, 12, 9, 0, 0),
        )


# --------------------------------------------------------------------------
# 断开
# --------------------------------------------------------------------------


def test_disconnect_clears_the_token_and_keeps_audit_fields() -> None:
    """断开清除密文，**保留历史游标与作用域**用于审计。"""
    record = _record()
    later = datetime(2026, 10, 13, 9, 0, 0, tzinfo=UTC)

    disconnected = disconnect_gmail_connection(record, later)

    assert disconnected.encrypted_refresh_token is None
    assert disconnected.status == "disconnected"
    assert disconnected.granted_scopes == record.granted_scopes
    assert disconnected.created_at == record.created_at
    assert disconnected.updated_at == "2026-10-13T09:00:00.000Z"


def test_disconnect_is_idempotent_and_returns_the_same_object() -> None:
    """**重复断开返回原对象**，不改写时间。

    否则"断开时间"会被每次重复请求不断后移，审计记录失去意义。
    """
    record = _record()
    first = disconnect_gmail_connection(record, NOW)
    second = disconnect_gmail_connection(
        first, datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)
    )

    assert second is first
    assert second.updated_at == "2026-10-12T09:00:00.000Z"


def test_disconnected_but_still_holding_a_token_is_cleaned() -> None:
    """状态已是断开但密文仍在时必须清除——不能因为状态就跳过清理。"""
    from dataclasses import replace

    record = replace(_record(), status="disconnected")

    cleaned = disconnect_gmail_connection(record, NOW)

    assert cleaned.encrypted_refresh_token is None
