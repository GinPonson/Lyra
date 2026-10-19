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
from threading import RLock
from typing import Any, Literal

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

_WHITESPACE = re.compile(r"\s+")
_HASH = re.compile(r"^[a-f0-9]{64}$")

GmailOAuthErrorCode = Literal[
    "GMAIL_SCOPE_MISSING",
    "GOOGLE_AUTH_TRANSACTION_USED",
    "GOOGLE_AUTH_TRANSACTION_EXPIRED",
    "GOOGLE_AUTH_TRANSACTION_INVALID",
    "GOOGLE_AUTH_TRANSACTION_CAPACITY",
]

_MAX_AUTHORIZATION_TRANSACTIONS = 1_024
_EXPIRED_TRANSACTION_RETENTION_MILLISECONDS = 10 * 60 * 1_000


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
    """仅存在于服务端短期内存中的一次性授权事务与轮询结果。"""

    state_hash: str
    # 交换授权码时使用；**不得返回 App、写入日志或长期持久化**。
    code_verifier: str
    authorization_id: str
    user_id: str
    device_id: str
    expires_at: str
    consumed_at: str | None
    status: Literal["pending", "processing", "complete", "failed"] = "pending"
    result: dict[str, Any] | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    failure_status_code: int | None = None
    failure_retryable: bool = False


@dataclass(frozen=True)
class CreatedGoogleOAuthTransaction:
    """创建事务时返回给 App 的公开参数与服务端私有记录。"""

    state: str
    code_challenge: str
    code_challenge_method: Literal["S256"]
    record: GoogleOAuthTransactionRecord


def create_google_oauth_transaction(
    device_id: str,
    now: datetime,
    ttl_milliseconds: int,
    *,
    user_id: str,
    authorization_id: str,
) -> CreatedGoogleOAuthTransaction:
    """创建绑定设备、短时且一次性的授权事务。

    @param device_id: 发起授权的已认证设备 ID。
    @param now: 服务端可信时钟。
    @param ttl_milliseconds: 事务有效期毫秒数。
    @param user_id: 发起授权的已认证用户 ID。
    @param authorization_id: App 用于轮询结果的独立 ID。
    @returns 公开参数与私有记录。
    @raises ValueError: 设备 ID 为空、有效期非法或时间不带时区。
    """
    resolved_device = device_id.strip()
    if resolved_device == "":
        raise ValueError("Google 授权事务 deviceId 不能为空")
    resolved_user = user_id.strip()
    if resolved_user == "":
        raise ValueError("Google 授权事务 userId 不能为空")
    resolved_authorization_id = authorization_id.strip()
    if resolved_authorization_id == "":
        raise ValueError("Google 授权事务 authorizationId 不能为空")
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
            authorization_id=resolved_authorization_id,
            user_id=resolved_user,
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


class GoogleOAuthTransactionStore:
    """以 state 摘要索引短期事务，并保留可重复读取的安全结果。"""

    def __init__(self) -> None:
        self._records: dict[str, GoogleOAuthTransactionRecord] = {}
        self._lock = RLock()

    def create(self, record: GoogleOAuthTransactionRecord, *, now: datetime) -> None:
        """保存新事务并先清理超过保留期的旧结果。

        @param record: 新事务记录；只含 state 摘要。
        @param now: 服务端可信时钟。
        @raises GmailOAuthError: 内存事务达到有界容量。
        """
        with self._lock:
            self._purge_expired(now)
            if len(self._records) >= _MAX_AUTHORIZATION_TRANSACTIONS:
                raise GmailOAuthError(
                    "GOOGLE_AUTH_TRANSACTION_CAPACITY",
                    "Google 授权请求过多，请稍后重试",
                    retryable=True,
                )
            self._records[record.state_hash] = record

    def claim(self, *, state: str, now: datetime) -> GoogleOAuthTransactionRecord:
        """原子消费一次回调 state，并返回仅供本次交换使用的 verifier。

        @param state: Google 回调携带的 state 原文。
        @param now: 服务端可信时钟。
        @returns 已进入处理态的事务记录。
        @raises GmailOAuthError: state 无效、过期或已使用。
        """
        try:
            state_digest = hash_state(state)
        except (AttributeError, UnicodeEncodeError):
            raise _invalid_transaction() from None
        with self._lock:
            self._purge_expired(now)
            record = self._records.get(state_digest)
            if record is None or not _matches_state(state, record.state_hash):
                raise _invalid_transaction()
            if record.status != "pending" or record.consumed_at is not None:
                raise GmailOAuthError(
                    "GOOGLE_AUTH_TRANSACTION_USED", "Google 授权事务已经使用"
                )
            if _ms(now) >= _ms(_parse(record.expires_at)):
                self._records[state_digest] = _failed_record(
                    record,
                    code="GOOGLE_AUTH_TRANSACTION_EXPIRED",
                    message="Google 授权事务已经过期",
                    status_code=410,
                    retryable=False,
                    now=now,
                )
                raise GmailOAuthError(
                    "GOOGLE_AUTH_TRANSACTION_EXPIRED", "Google 授权事务已经过期"
                )
            claimed = replace(record, status="processing", consumed_at=_iso(now))
            # verifier 只留在当前调用栈；共享状态在回调兑换开始时即清除它。
            self._records[state_digest] = replace(claimed, code_verifier="")
            return claimed

    def complete(
        self, *, state_hash: str, connection: dict[str, Any]
    ) -> None:
        """记录成功连接，供原设备在 TTL 内重复读取。

        @param state_hash: 服务端保存的 state 摘要。
        @param connection: 已去除凭据的公开连接视图。
        """
        with self._lock:
            record = self._records.get(state_hash)
            if record is None or record.status != "processing":
                return
            self._records[state_hash] = replace(
                record, status="complete", result=connection,
                code_verifier="", failure_code=None, failure_message=None,
            )

    def fail(
        self,
        *,
        state_hash: str,
        code: str,
        message: str,
        status_code: int,
        retryable: bool,
    ) -> None:
        """记录安全失败，不保存上游异常文本或 OAuth 凭据。

        @param state_hash: 服务端保存的 state 摘要。
        @param code: 面向 App 的稳定错误码。
        @param message: 不包含凭据的说明。
        @param status_code: 轮询响应状态码。
        @param retryable: 同一授权结果是否适合重试。
        """
        with self._lock:
            record = self._records.get(state_hash)
            if record is None or record.status != "processing":
                return
            self._records[state_hash] = _failed_record(
                record,
                code=code,
                message=message,
                status_code=status_code,
                retryable=retryable,
                now=None,
            )

    def get(
        self,
        *,
        authorization_id: str,
        user_id: str,
        device_id: str,
        now: datetime,
    ) -> GoogleOAuthTransactionRecord:
        """按授权 ID 读取事务，仅允许创建它的用户和设备访问。

        @param authorization_id: App 轮询 ID。
        @param user_id: 当前认证用户 ID。
        @param device_id: 当前认证设备 ID。
        @param now: 服务端可信时钟。
        @returns 事务及当前状态。
        @raises GmailOAuthError: 不存在、绑定不符或超过 TTL。
        """
        with self._lock:
            self._purge_expired(now)
            record = next(
                (item for item in self._records.values()
                 if item.authorization_id == authorization_id),
                None,
            )
            if record is None or record.user_id != user_id or record.device_id != device_id:
                raise _invalid_transaction()
            if _ms(now) >= _ms(_parse(record.expires_at)):
                raise GmailOAuthError(
                    "GOOGLE_AUTH_TRANSACTION_EXPIRED", "Google 授权事务已经过期"
                )
            return record

    def _purge_expired(self, now: datetime) -> None:
        """删除过期后额外保留期也已结束的事务，限制内存增长。"""
        cutoff = _ms(now) - _EXPIRED_TRANSACTION_RETENTION_MILLISECONDS
        expired = [
            key for key, record in self._records.items()
            if _ms(_parse(record.expires_at)) < cutoff
        ]
        for key in expired:
            del self._records[key]


def _failed_record(
    record: GoogleOAuthTransactionRecord,
    *,
    code: str,
    message: str,
    status_code: int,
    retryable: bool,
    now: datetime | None,
) -> GoogleOAuthTransactionRecord:
    """转换成只含安全错误信息的终态记录。"""
    return replace(
        record,
        code_verifier="",
        consumed_at=record.consumed_at or (None if now is None else _iso(now)),
        status="failed",
        result=None,
        failure_code=code,
        failure_message=message,
        failure_status_code=status_code,
        failure_retryable=retryable,
    )


def _invalid_transaction() -> GmailOAuthError:
    """构造不区分未知 ID 与跨用户/设备访问的错误。"""
    return GmailOAuthError(
        "GOOGLE_AUTH_TRANSACTION_INVALID", "Google 授权事务无效"
    )


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
