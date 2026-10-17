"""Gmail 连接的凭据处理。

镜像 TypeScript 版 ``infrastructure/gmail/connection-credential.ts``。

两处安全性质：

1. **refresh token 的密文绑定连接 ID 作为 AAD。** 换一个连接 ID 解密就会失败，
   因此密文**无法在连接之间搬运**——否则一次数据库写入错误就能让 A 连接的凭据
   被 B 连接使用。
2. **必须包含 Gmail 只读作用域。** 少了它连接根本不能用；更重要的是，作用域文本
   会被规范化（去重 + 排序）后存储，避免同一组权限因顺序不同而产生不同记录。

断开连接**幂等**，且保留历史游标与消息元数据用于审计——只清除能继续访问邮箱的
那份 refresh token 密文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .oauth_transaction import (
    GMAIL_READONLY_SCOPE,
    GmailOAuthError,
    assert_gmail_readonly_scope,
)

__all__ = [
    "GMAIL_READONLY_SCOPE",
    "GmailConnectionRecord",
    "GmailConnectionStatus",
    "GmailOAuthError",
    "GoogleOfflineGrant",
    "VerifiedGoogleIdentity",
    "assert_gmail_readonly_scope",
    "create_gmail_connection_record",
    "disconnect_gmail_connection",
]

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")

GmailConnectionStatus = Literal["pending_initial_scan", "connected", "disconnected", "error"]


@dataclass(frozen=True)
class VerifiedGoogleIdentity:
    """已验证的 Google 身份。"""

    subject: str
    email: str
    email_verified: bool


@dataclass(frozen=True)
class GoogleOfflineGrant:
    """离线授权结果。"""

    refresh_token: str
    granted_scope_text: str


@dataclass(frozen=True)
class GmailConnectionRecord:
    """与 ``gmail_connections`` 表对齐的记录。"""

    id: str
    google_subject: str
    google_email: str
    encrypted_refresh_token: str | None
    granted_scopes: str
    history_id: str | None
    status: GmailConnectionStatus
    last_error_code: str | None
    last_sync_at: str | None
    created_at: str
    updated_at: str


def create_gmail_connection_record(
    *,
    connection_id: str,
    identity: VerifiedGoogleIdentity,
    grant: GoogleOfflineGrant,
    cipher: object,
    now: datetime,
) -> GmailConnectionRecord:
    """创建一条 Gmail 连接记录。

    @param connection_id: 连接 ID。
    @param identity: 已验证的 Google 身份。
    @param grant: 离线授权结果。
    @param cipher: 实现了 ``encrypt(plaintext, context)`` 的加密器。
    @param now: 服务端可信时间。
    @returns 可持久化的连接记录。
    @raises ValueError: 字段缺失或时间非法。
    @raises GmailOAuthError: 邮箱未验证或缺少只读作用域。
    """
    identifier = _require_text(connection_id, "Gmail 连接 ID")
    subject = _require_text(identity.subject, "Google subject")
    email = _require_text(identity.email, "Google 邮箱").lower()
    if not identity.email_verified or not _EMAIL.fullmatch(email):
        # 未验证的邮箱不能作为身份依据：任何人都可以把别人的邮箱填进自己的资料。
        raise ValueError("Google 邮箱身份尚未验证或格式无效")
    refresh_token = _require_text(grant.refresh_token, "Google refresh token")
    scopes = assert_gmail_readonly_scope(grant.granted_scope_text)
    timestamp = _require_timestamp(now)

    return GmailConnectionRecord(
        id=identifier,
        google_subject=subject,
        google_email=email,
        # AAD 绑定连接 ID：密文无法在连接之间搬运。
        encrypted_refresh_token=cipher.encrypt(  # type: ignore[attr-defined]
            refresh_token, f"gmail-connection:{identifier}"
        ),
        granted_scopes=" ".join(scopes),
        history_id=None,
        status="pending_initial_scan",
        last_error_code=None,
        last_sync_at=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


def disconnect_gmail_connection(
    connection: GmailConnectionRecord, now: datetime
) -> GmailConnectionRecord:
    """断开 Gmail 并清除 refresh token 密文。

    **幂等**：已经断开且密文已清空时返回**原对象**，不改写时间——重复断开不应
    让"断开时间"不断后移。

    历史游标与既有消息元数据**保留**用于审计。

    @param connection: 当前连接。
    @param now: 服务端可信时间。
    @returns 断开后的连接，或原对象。
    """
    if connection.status == "disconnected" and connection.encrypted_refresh_token is None:
        return connection
    return GmailConnectionRecord(
        id=connection.id,
        google_subject=connection.google_subject,
        google_email=connection.google_email,
        encrypted_refresh_token=None,
        granted_scopes=connection.granted_scopes,
        history_id=connection.history_id,
        status="disconnected",
        last_error_code=None,
        last_sync_at=connection.last_sync_at,
        created_at=connection.created_at,
        updated_at=_require_timestamp(now),
    )


def _require_text(value: str, label: str) -> str:
    """去除边界空白并拒绝缺失的可信字段。"""
    if not isinstance(value, str):
        raise ValueError(f"{label}不能为空")
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{label}不能为空")
    return normalized


def _require_timestamp(now: datetime) -> str:
    """校验时钟并生成 UTC ISO 时间戳。

    @raises ValueError: 时间不带时区。
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("Gmail 连接时间必须带时区")
    return (
        now.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
