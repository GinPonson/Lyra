"""Google 授权事务测试。

三条安全性质：

1. **只存 state 的哈希**，明文只返回一次；
2. **设备不匹配与 state 不匹配合并报错**——分开会把暴力搜索变成两步；
3. **检查顺序是「已用 → 过期 → 无效」**——已用且同时过期的事务报"已使用"，
   重放信号优先于过期，否则一次真实重放会被当成普通的会话超时。
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from lyra.infrastructure.gmail.oauth_transaction import (
    GMAIL_READONLY_SCOPE,
    GmailOAuthError,
    assert_gmail_readonly_scope,
    consume_google_oauth_transaction,
    create_google_oauth_transaction,
    hash_state,
)

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
TTL_MS = 10 * 60 * 1000
DEVICE = "device-1"


def _created():
    return create_google_oauth_transaction(DEVICE, NOW, TTL_MS)


def _consume(record, *, state, device_id=DEVICE, now=NOW):
    return consume_google_oauth_transaction(
        record, state=state, device_id=device_id, now=now
    )


# --------------------------------------------------------------------------
# 创建
# --------------------------------------------------------------------------


def test_creates_a_transaction_with_public_and_private_parts() -> None:
    """公开参数与私有记录分开给出。"""
    created = _created()

    assert len(created.state) == 43
    assert created.code_challenge_method == "S256"
    # 记录里**只有 state 的哈希**。
    assert created.record.state_hash == hash_state(created.state)
    assert created.state not in created.record.state_hash
    assert created.record.consumed_at is None
    assert created.record.device_id == DEVICE
    assert created.record.expires_at == "2026-10-12T09:10:00.000Z"


def test_code_challenge_is_the_s256_challenge_of_the_verifier() -> None:
    """挑战是 verifier 的 S256 摘要（base64url 无填充）。"""
    created = _created()
    expected = (
        base64.urlsafe_b64encode(
            hashlib.sha256(created.record.code_verifier.encode("ascii")).digest()
        )
        .rstrip(b"=")
        .decode()
    )

    assert created.code_challenge == expected


def test_state_and_verifier_are_independent_random_values() -> None:
    """两次事务的 state 与 verifier 都不同。"""
    first, second = _created(), _created()

    assert first.state != second.state
    assert first.record.code_verifier != second.record.code_verifier


@pytest.mark.parametrize("ttl", [0, -1, 1.5, True])
def test_rejects_invalid_ttl(ttl: object) -> None:
    """有效期必须是正整数。"""
    with pytest.raises(ValueError, match="正安全整数"):
        create_google_oauth_transaction(DEVICE, NOW, ttl)  # type: ignore[arg-type]


def test_rejects_blank_device() -> None:
    """设备 ID 不能为空。"""
    with pytest.raises(ValueError, match="deviceId 不能为空"):
        create_google_oauth_transaction("  ", NOW, TTL_MS)


def test_rejects_naive_time() -> None:
    """时间必须带时区。"""
    with pytest.raises(ValueError, match="必须带时区"):
        create_google_oauth_transaction(
            DEVICE, datetime(2026, 10, 12, 9, 0, 0), TTL_MS
        )


# --------------------------------------------------------------------------
# 消费
# --------------------------------------------------------------------------


def test_consume_marks_the_transaction_used() -> None:
    """消费写入 consumed_at 并返回新对象。"""
    created = _created()

    consumed = _consume(created.record, state=created.state)

    assert consumed.consumed_at == "2026-10-12T09:00:00.000Z"
    assert created.record.consumed_at is None
    assert consumed is not created.record


def test_replay_is_rejected() -> None:
    """同一 state 不能重放。"""
    created = _created()
    consumed = _consume(created.record, state=created.state)

    with pytest.raises(GmailOAuthError) as raised:
        _consume(consumed, state=created.state)

    assert raised.value.code == "GOOGLE_AUTH_TRANSACTION_USED"


def test_expiry_is_enforced_at_the_boundary() -> None:
    """到期时刻（含等于）即拒绝。"""
    created = _created()
    at_expiry = NOW + timedelta(milliseconds=TTL_MS)

    with pytest.raises(GmailOAuthError) as raised:
        _consume(created.record, state=created.state, now=at_expiry)
    assert raised.value.code == "GOOGLE_AUTH_TRANSACTION_EXPIRED"

    # 差一毫秒仍有效。
    just_before = at_expiry - timedelta(milliseconds=1)
    assert _consume(created.record, state=created.state, now=just_before).consumed_at


def test_used_takes_priority_over_expired() -> None:
    """**已用且同时过期时报"已使用"。**

    把它当"过期"会让一次真实重放看起来像普通的会话超时——重放的信号被掩盖了。
    """
    created = _created()
    consumed = _consume(created.record, state=created.state)
    long_after = NOW + timedelta(days=365)

    with pytest.raises(GmailOAuthError) as raised:
        _consume(consumed, state=created.state, now=long_after)

    assert raised.value.code == "GOOGLE_AUTH_TRANSACTION_USED"


def test_wrong_state_reports_invalid() -> None:
    """state 不匹配报无效。"""
    created = _created()
    other = _created()

    with pytest.raises(GmailOAuthError) as raised:
        _consume(created.record, state=other.state)
    assert raised.value.code == "GOOGLE_AUTH_TRANSACTION_INVALID"


def test_wrong_device_reports_the_same_error_as_wrong_state() -> None:
    """**设备不匹配与 state 不匹配必须报同一个错误码。**

    分开报会把暴力搜索变成两步：先确定"state 猜对了但设备不对"，
    再针对设备继续猜。
    """
    created = _created()

    with pytest.raises(GmailOAuthError) as by_state:
        _consume(created.record, state=_created().state)
    with pytest.raises(GmailOAuthError) as by_device:
        _consume(created.record, state=created.state, device_id="other")

    assert by_state.value.code == by_device.value.code
    assert by_state.value.message == by_device.value.message


def test_malformed_state_hash_is_rejected() -> None:
    """记录里的哈希格式非法时一律无效，而不是抛底层异常。"""
    from dataclasses import replace

    created = _created()
    broken = replace(created.record, state_hash="not-a-hash")

    with pytest.raises(GmailOAuthError) as raised:
        _consume(broken, state=created.state)
    assert raised.value.code == "GOOGLE_AUTH_TRANSACTION_INVALID"


def test_non_ascii_state_is_rejected_without_raising_unicode_error() -> None:
    """非 ASCII 的 state 不可能是我们生成的，必须报无效而不是崩在编码上。"""
    created = _created()

    with pytest.raises(GmailOAuthError) as raised:
        _consume(created.record, state="状态" * 10)
    assert raised.value.code == "GOOGLE_AUTH_TRANSACTION_INVALID"


# --------------------------------------------------------------------------
# 作用域
# --------------------------------------------------------------------------


def test_scope_validation_normalizes() -> None:
    """作用域去重排序。"""
    scopes = assert_gmail_readonly_scope(
        f"https://b.example  https://a.example {GMAIL_READONLY_SCOPE} https://b.example"
    )

    assert scopes == (
        "https://a.example", "https://b.example", GMAIL_READONLY_SCOPE
    )


def test_missing_scope_is_rejected() -> None:
    """缺少只读作用域必须拒绝。"""
    with pytest.raises(GmailOAuthError) as raised:
        assert_gmail_readonly_scope("https://www.googleapis.com/auth/userinfo.email")

    assert raised.value.code == "GMAIL_SCOPE_MISSING"


def test_scope_errors_are_not_retryable_by_default() -> None:
    """默认不可重试：重试不会改变授权结果。"""
    with pytest.raises(GmailOAuthError) as raised:
        assert_gmail_readonly_scope("")
    assert raised.value.retryable is False
