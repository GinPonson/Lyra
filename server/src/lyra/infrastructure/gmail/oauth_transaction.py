"""Google 离线授权事务。

镜像 TypeScript 版 ``infrastructure/gmail/oauth-transaction.ts``。

**这是一次性短时事务**，绑定发起它的设备。三处安全性质：

1. **只存 state 的哈希**，明文 state 只返回一次；
2. **state 不匹配与设备不匹配合并为同一个错误码**——分开报会让攻击者知道
   "state 猜对了但设备不对"，把暴力搜索变成两步；
3. **检查顺序是「已用 → 过期 → 无效」**。已用且同时过期的事务报"已使用"——
   重放信号必须优先于过期，否则一次真实重放会被当成普通的会话过期。

`code_verifier` **不得返回 App、写入日志或长期持久化**：它只在交换授权码时使用一次。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

_WHITESPACE = re.compile(r"\s+")
_HASH = re.compile(r"^[a-f0-9]{64}$")

GmailOAuthErrorCode = Literal[
    "GMAIL_SCOPE_MISSING",
    "GOOGLE_AUTH_TRANSACTION_USED",
    "GOOGLE_AUTH_TRANSACTION_EXPIRED",
    "GOOGLE_AUTH_TRANSACTION_INVALID",
]


class GmailOAuthError(Exception):
    """Google 授权事务或实际授权范围无效。

    @param code: 客户端可稳定分支处理的错误码。
    @param message: 不含凭据的安全说明。
    @param retryable: 是否值得重试。
    """

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class GoogleOAuthTransactionRecord:
    """仅存在于服务端短期内存中的一次性授权事务。"""

    state_hash: str
    # 交换授权码时使用；**不得返回 App、写入日志或长期持久化**。
    code_verifier: str
    device_id: str
    expires_at: str
    consumed_at: str | None


@dataclass(frozen=True)
class CreatedGoogleOAuthTransaction:
    """创建事务时返回给 App 的公开参数与服务端私有记录。"""

    state: str
    code_challenge: str
    code_challenge_method: Literal["S256"]
    record: GoogleOAuthTransactionRecord


def create_google_oauth_transaction(
    device_id: str, now: datetime, ttl_milliseconds: int
) -> CreatedGoogleOAuthTransaction:
    """创建绑定设备、短时且一次性的授权事务。

    @param device_id: 发起授权的已认证设备 ID。
    @param now: 服务端可信时钟。
    @param ttl_milliseconds: 事务有效期毫秒数。
    @returns 公开参数与私有记录。
    @raises ValueError: 设备 ID 为空、有效期非法或时间不带时区。
    """
    resolved_device = device_id.strip()
    if resolved_device == "":
        raise ValueError("Google 授权事务 deviceId 不能为空")
    if (
        not isinstance(ttl_milliseconds, int)
        or isinstance(ttl_milliseconds, bool)
        or ttl_milliseconds <= 0
    ):
        raise ValueError("Google 授权事务有效期必须是正安全整数毫秒数")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("Google 授权事务时间必须带时区")

    state = _random_base64url(32)
    code_verifier = _random_base64url(32)
    return CreatedGoogleOAuthTransaction(
        state=state,
        # 输入按 ASCII 编码：PKCE verifier 的字符集本就限定在 ASCII 可见范围内。
        code_challenge=_pkce_challenge(code_verifier),
        code_challenge_method="S256",
        record=GoogleOAuthTransactionRecord(
            state_hash=hash_state(state),
            code_verifier=code_verifier,
            device_id=resolved_device,
            expires_at=_iso(now + timedelta(milliseconds=ttl_milliseconds)),
            consumed_at=None,
        ),
    )


def consume_google_oauth_transaction(
    record: GoogleOAuthTransactionRecord,
    *,
    state: str,
    device_id: str,
    now: datetime,
) -> GoogleOAuthTransactionRecord:
    """校验设备、state 与过期时间后一次性消费事务。

    返回值必须与授权码交换的开始状态**原子保存**；同一 state 不能重放。

    @param record: 事务记录。
    @param state: 回调携带的 state 原文。
    @param device_id: 发起兑换的设备 ID。
    @param now: 服务端可信时间。
    @returns 已消费的新记录。
    @raises GmailOAuthError: 已使用、已过期或无效。
    """
    if record.consumed_at is not None:
        # **重放优先于过期**：把它当"过期"会让一次真实重放看起来像普通的会话超时。
        raise GmailOAuthError("GOOGLE_AUTH_TRANSACTION_USED", "Google 授权事务已经使用")
    if _ms(now) >= _ms(_parse(record.expires_at)):
        raise GmailOAuthError(
            "GOOGLE_AUTH_TRANSACTION_EXPIRED", "Google 授权事务已经过期"
        )
    # **设备不匹配与 state 不匹配合并**：分开报会把暴力搜索变成两步——先确定
    # "state 猜对了但设备不对"。
    if device_id != record.device_id or not _matches_state(state, record.state_hash):
        raise GmailOAuthError(
            "GOOGLE_AUTH_TRANSACTION_INVALID", "Google 授权事务无效"
        )
    return replace(record, consumed_at=_iso(now))


def assert_gmail_readonly_scope(granted_scope_text: str) -> tuple[str, ...]:
    """校验并规范化授权作用域。

    去重并**排序**后存储：同一组权限不应因顺序不同而产生不同记录。

    @param granted_scope_text: Google 返回的空格分隔 scope 字符串。
    @returns 去重排序后的作用域。
    @raises GmailOAuthError: 缺少 Gmail 只读作用域。
    """
    scopes = sorted(set(filter(None, _WHITESPACE.split(granted_scope_text))))
    if GMAIL_READONLY_SCOPE not in scopes:
        raise GmailOAuthError(
            "GMAIL_SCOPE_MISSING", "Google 授权未包含 Gmail 只读权限"
        )
    return tuple(scopes)


def hash_state(state: str) -> str:
    """对高熵 state 生成仅服务端保存的 SHA-256 摘要。

    输入按 **ASCII** 编码，与现有实现一致。
    """
    return hashlib.sha256(state.encode("ascii", errors="strict")).hexdigest()


def _matches_state(state: str, expected_hash: str) -> bool:
    """常量时间比较 state 与期望哈希。"""
    if not _HASH.fullmatch(expected_hash):
        return False
    try:
        digest = hash_state(state)
    except UnicodeEncodeError:
        # 非 ASCII 的 state 不可能是我们生成的。
        return False
    return hmac.compare_digest(digest, expected_hash)


def _pkce_challenge(verifier: str) -> str:
    """计算 S256 挑战：base64url(sha256(verifier))，无填充。"""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _random_base64url(byte_count: int) -> str:
    """生成无填充 base64url 随机值。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(byte_count)).rstrip(b"=").decode("ascii")


def _iso(value: datetime) -> str:
    """渲染为与 JavaScript ``toISOString()`` 同形的文本。"""
    return (
        value.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parse(value: str) -> datetime:
    """解析 ISO 时间戳。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _ms(value: datetime) -> float:
    """折算为毫秒。"""
    return value.timestamp() * 1000
