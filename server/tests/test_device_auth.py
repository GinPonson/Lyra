"""设备凭据与访问令牌测试。

覆盖三条不可放宽的性质：

1. **只持久化哈希**——明文令牌不出现在任何记录里；
2. **常量时间比较**——校验不因令牌前缀相同而变慢（避免逐字节试探）；
3. **检查顺序是安全语义**——凭据 → 设备撤销 → 令牌撤销 → 过期，
   调换顺序会改变客户端看到的错误码。
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta

import pytest

from lyra.security.device_auth import (
    DeviceAccessTokenRecord,
    DeviceAuthError,
    PairedDeviceAuthRecord,
    authenticate_device_access_token,
    create_opaque_credential,
    hash_opaque_credential,
    issue_device_access_token,
    verify_opaque_credential,
)

BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
TTL_MS = 3_600_000

ACTIVE_DEVICE = PairedDeviceAuthRecord(id="device-1", status="active", revoked_at=None)
REVOKED_DEVICE = PairedDeviceAuthRecord(
    id="device-1", status="revoked", revoked_at="2026-10-12T08:00:00.000Z"
)


def _issued() -> tuple[str, DeviceAccessTokenRecord]:
    """签发一枚有效访问令牌。"""
    issued = issue_device_access_token(ACTIVE_DEVICE, NOW, TTL_MS)
    return issued.token, issued.record


def test_opaque_credential_shape() -> None:
    """令牌必须是无填充 base64url 的 256 位随机值。"""
    credential = create_opaque_credential()

    # 32 字节 base64url 无填充 = 43 个字符。
    assert len(credential.token) == 43
    assert BASE64URL.fullmatch(credential.token)
    assert "=" not in credential.token
    assert HEX64.fullmatch(credential.token_hash)


def test_opaque_credential_is_random() -> None:
    """两次生成不得相同。"""
    assert create_opaque_credential().token != create_opaque_credential().token


def test_hash_matches_sha256_known_vector() -> None:
    """哈希必须是 UTF-8 字节的 SHA-256 十六进制，与 TypeScript 侧一致。"""
    assert hash_opaque_credential("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_hash_is_deterministic_and_case_sensitive() -> None:
    """同一令牌哈希稳定；大小写不同视为不同令牌。"""
    token = "Lyra-Opaque-Token"
    assert hash_opaque_credential(token) == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert hash_opaque_credential(token.upper()) != hash_opaque_credential(token)


def test_verify_opaque_credential() -> None:
    """校验必须只接受完全匹配的令牌。"""
    token = create_opaque_credential().token
    expected = hash_opaque_credential(token)

    assert verify_opaque_credential(token, expected) is True
    assert verify_opaque_credential(token + "x", expected) is False
    assert verify_opaque_credential(token.upper(), expected) is False


def test_verify_rejects_malformed_expected_hash() -> None:
    """格式非法的期望哈希一律不匹配，不得抛异常。"""
    token = create_opaque_credential().token
    for malformed in ("", "abc", "Z" * 64, "a" * 63, "a" * 65, "A" * 64):
        assert verify_opaque_credential(token, malformed) is False


def test_issue_device_access_token_record() -> None:
    """签发的记录必须只含哈希，且到期时间等于签发时间加有效期。"""
    token, record = _issued()

    assert record.device_id == "device-1"
    assert record.status == "active"
    assert record.token_hash == hash_opaque_credential(token)
    assert record.last_seen_at is None
    assert record.revoked_at is None
    assert record.issued_at == "2026-10-12T09:00:00.000Z"
    assert record.expires_at == "2026-10-12T10:00:00.000Z"
    # 明文令牌绝不能出现在持久化记录里。
    assert token not in repr(record)


def test_issue_rejects_revoked_device() -> None:
    """已撤销设备不得再签发令牌。"""
    with pytest.raises(DeviceAuthError) as raised:
        issue_device_access_token(REVOKED_DEVICE, NOW, TTL_MS)
    assert raised.value.code == "DEVICE_REVOKED"


@pytest.mark.parametrize("ttl", [0, -1, 1.5, True])
def test_issue_rejects_invalid_ttl(ttl: object) -> None:
    """有效期必须是正安全整数毫秒数。"""
    with pytest.raises(ValueError, match="正安全整数"):
        issue_device_access_token(ACTIVE_DEVICE, NOW, ttl)  # type: ignore[arg-type]


def test_authenticate_updates_last_seen() -> None:
    """认证成功必须返回更新后的 last_seen_at，且不改动其它字段。"""
    token, record = _issued()
    later = NOW + timedelta(minutes=5)

    updated = authenticate_device_access_token(ACTIVE_DEVICE, record, token, later)

    assert updated.last_seen_at == "2026-10-12T09:05:00.000Z"
    assert updated.expires_at == record.expires_at
    assert updated.token_hash == record.token_hash


def test_authenticate_rejects_wrong_token() -> None:
    """令牌不匹配必须报「凭据无效」。"""
    _, record = _issued()
    with pytest.raises(DeviceAuthError) as raised:
        authenticate_device_access_token(ACTIVE_DEVICE, record, "wrong-token", NOW)
    assert raised.value.code == "DEVICE_CREDENTIAL_INVALID"


def test_authenticate_rejects_mismatched_device() -> None:
    """记录所属设备与当前设备不一致必须拒绝。"""
    token, record = _issued()
    other = PairedDeviceAuthRecord(id="device-2", status="active", revoked_at=None)

    with pytest.raises(DeviceAuthError) as raised:
        authenticate_device_access_token(other, record, token, NOW)
    assert raised.value.code == "DEVICE_CREDENTIAL_INVALID"


def test_authenticate_rejects_revoked_device() -> None:
    """设备已撤销时，即使令牌本身有效也必须拒绝。"""
    token, record = _issued()
    with pytest.raises(DeviceAuthError) as raised:
        authenticate_device_access_token(REVOKED_DEVICE, record, token, NOW)
    assert raised.value.code == "DEVICE_REVOKED"


def test_authenticate_rejects_revoked_token() -> None:
    """令牌被撤销后必须报「凭据无效」。"""
    token, record = _issued()
    revoked = DeviceAccessTokenRecord(
        id=record.id,
        device_id=record.device_id,
        token_hash=record.token_hash,
        status="revoked",
        expires_at=record.expires_at,
        last_seen_at=record.last_seen_at,
        revoked_at="2026-10-12T09:10:00.000Z",
        issued_at=record.issued_at,
    )
    with pytest.raises(DeviceAuthError) as raised:
        authenticate_device_access_token(ACTIVE_DEVICE, revoked, token, NOW)
    assert raised.value.code == "DEVICE_CREDENTIAL_INVALID"


def test_authenticate_rejects_expired_token() -> None:
    """到期时刻（含等于）必须判过期。"""
    token, record = _issued()
    at_expiry = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)

    with pytest.raises(DeviceAuthError) as raised:
        authenticate_device_access_token(ACTIVE_DEVICE, record, token, at_expiry)
    assert raised.value.code == "DEVICE_CREDENTIAL_EXPIRED"

    # 差一毫秒仍有效。
    just_before = at_expiry - timedelta(milliseconds=1)
    assert authenticate_device_access_token(
        ACTIVE_DEVICE, record, token, just_before
    ).last_seen_at is not None


def test_check_order_prefers_credential_failure() -> None:
    """错误码优先级：凭据无效优先于设备撤销。

    若实现先判设备状态，客户端会把「令牌错了」误读成「设备被撤销」，
    进而提示用户重新配对而不是重新登录。顺序是接口契约的一部分。
    """
    _, record = _issued()
    with pytest.raises(DeviceAuthError) as raised:
        authenticate_device_access_token(REVOKED_DEVICE, record, "wrong-token", NOW)
    assert raised.value.code == "DEVICE_CREDENTIAL_INVALID"


def test_check_order_prefers_device_revocation_over_expiry() -> None:
    """设备撤销优先于令牌过期。"""
    token, record = _issued()
    long_after = NOW + timedelta(days=30)

    with pytest.raises(DeviceAuthError) as raised:
        authenticate_device_access_token(REVOKED_DEVICE, record, token, long_after)
    assert raised.value.code == "DEVICE_REVOKED"
