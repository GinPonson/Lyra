"""设备刷新凭据的签发、轮换与重放撤销测试。

刷新凭据是长期凭据，一旦泄漏影响远大于短期访问令牌。本文件重点覆盖：

1. **只持久化哈希**；
2. **检查顺序**——凭据状态先于过期，使重放的已用令牌按「重用」而非「过期」报告；
3. **重放后撤销整族**——包括活动的最新代，否则攻击者与合法客户端会持续竞速；
4. **撤销计划严格限定同设备同族**，避免一个设备的泄漏波及其它设备。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from lyra.security.device_auth import (
    DeviceAuthError,
    DeviceRefreshCredentialRecord,
    PairedDeviceAuthRecord,
    hash_opaque_credential,
    issue_device_refresh_credential,
    plan_refresh_credential_reuse_revocation,
    rotate_device_refresh_credential,
)

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
TTL_MS = 30 * 24 * 3_600_000

DEVICE = PairedDeviceAuthRecord(id="device-1", status="active", revoked_at=None)
REVOKED_DEVICE = PairedDeviceAuthRecord(
    id="device-1", status="revoked", revoked_at="2026-10-12T08:00:00.000Z"
)


def _issued() -> tuple[str, DeviceRefreshCredentialRecord]:
    """签发一枚第一代刷新凭据。"""
    issued = issue_device_refresh_credential(DEVICE.id, NOW, TTL_MS)
    return issued.token, issued.record


def test_issue_creates_first_generation() -> None:
    """首发记录必须是第 1 代、新族、active，且只含哈希。"""
    token, record = _issued()

    assert record.device_id == "device-1"
    assert record.generation == 1
    assert record.status == "active"
    assert record.token_hash == hash_opaque_credential(token)
    assert record.last_used_at is None
    assert record.rotated_at is None
    assert record.revoked_at is None
    assert record.created_at == "2026-10-12T09:00:00.000Z"
    assert token not in repr(record)


def test_issue_uses_distinct_families() -> None:
    """每次配对必须开新族，否则一次泄漏会牵连其它配对。"""
    _, first = _issued()
    _, second = _issued()
    assert first.family_id != second.family_id


def test_rotate_advances_generation_in_same_family() -> None:
    """轮换后：旧记录转 rotated，新记录同族、代号 +1。"""
    token, record = _issued()
    later = NOW + timedelta(days=1)

    rotated = rotate_device_refresh_credential(DEVICE, record, token, later, TTL_MS)

    assert rotated.previous.status == "rotated"
    assert rotated.previous.last_used_at == "2026-10-13T09:00:00.000Z"
    assert rotated.previous.rotated_at == "2026-10-13T09:00:00.000Z"
    assert rotated.previous.id == record.id

    assert rotated.next.status == "active"
    assert rotated.next.family_id == record.family_id
    assert rotated.next.generation == record.generation + 1
    assert rotated.next.id != record.id
    assert rotated.next.token_hash == hash_opaque_credential(rotated.token)
    assert rotated.token != token


def test_rotate_rejects_wrong_token() -> None:
    """令牌不匹配必须报「凭据无效」。"""
    _, record = _issued()
    with pytest.raises(DeviceAuthError) as raised:
        rotate_device_refresh_credential(DEVICE, record, "wrong", NOW, TTL_MS)
    assert raised.value.code == "DEVICE_CREDENTIAL_INVALID"


def test_rotate_rejects_other_device() -> None:
    """记录所属设备不一致必须拒绝。"""
    token, record = _issued()
    other = PairedDeviceAuthRecord(id="device-2", status="active", revoked_at=None)

    with pytest.raises(DeviceAuthError) as raised:
        rotate_device_refresh_credential(other, record, token, NOW, TTL_MS)
    assert raised.value.code == "DEVICE_CREDENTIAL_INVALID"


def test_rotate_rejects_revoked_device() -> None:
    """设备撤销后即使令牌有效也必须拒绝。"""
    token, record = _issued()
    with pytest.raises(DeviceAuthError) as raised:
        rotate_device_refresh_credential(REVOKED_DEVICE, record, token, NOW, TTL_MS)
    assert raised.value.code == "DEVICE_REVOKED"


def test_rotate_reports_reuse_before_expiry() -> None:
    """**关键顺序**：已轮换的令牌即使同时过期，也必须报「重用」。

    报「过期」会引导客户端静默重新登录，从而掩盖一次真实的令牌重放；
    报「重用」才能触发整族撤销。
    """
    token, record = _issued()
    used = replace(record, status="rotated", rotated_at="2026-10-12T10:00:00.000Z")
    long_after = NOW + timedelta(days=365)

    with pytest.raises(DeviceAuthError) as raised:
        rotate_device_refresh_credential(DEVICE, used, token, long_after, TTL_MS)
    assert raised.value.code == "DEVICE_CREDENTIAL_REUSED"


def test_rotate_reports_expired_for_untouched_active_credential() -> None:
    """从未使用但已过期的凭据报「过期」，不是「重用」。"""
    token, record = _issued()
    long_after = NOW + timedelta(days=365)

    with pytest.raises(DeviceAuthError) as raised:
        rotate_device_refresh_credential(DEVICE, record, token, long_after, TTL_MS)
    assert raised.value.code == "DEVICE_CREDENTIAL_EXPIRED"


def test_reuse_revocation_revokes_whole_family() -> None:
    """整族撤销必须包含活动的最新代。"""
    token, first = _issued()
    rotated = rotate_device_refresh_credential(DEVICE, first, token, NOW, TTL_MS)
    family = (rotated.previous, rotated.next)

    plan = plan_refresh_credential_reuse_revocation(rotated.previous, family, NOW)

    assert plan.device_id == "device-1"
    assert plan.family_id == first.family_id
    assert all(credential.status == "revoked" for credential in plan.credentials)
    assert all(credential.revoked_at is not None for credential in plan.credentials)
    # 活动的最新代同样被撤销，否则攻击者仍可用它继续刷新。
    assert plan.credentials[1].id == rotated.next.id


def test_reuse_revocation_is_idempotent_for_revoked_rows() -> None:
    """已撤销的记录保持原时间戳不变。"""
    token, first = _issued()
    rotated = rotate_device_refresh_credential(DEVICE, first, token, NOW, TTL_MS)
    already_revoked = replace(
        rotated.previous, status="revoked", revoked_at="2026-10-01T00:00:00.000Z"
    )
    family = (already_revoked, rotated.next)

    plan = plan_refresh_credential_reuse_revocation(already_revoked, family, NOW)

    assert plan.credentials[0].revoked_at == "2026-10-01T00:00:00.000Z"


def test_reuse_revocation_rejects_active_credential() -> None:
    """活动凭据不能作为重用依据。"""
    _, record = _issued()
    with pytest.raises(ValueError, match="活动刷新凭据"):
        plan_refresh_credential_reuse_revocation(record, (record,), NOW)


def test_reuse_revocation_rejects_foreign_family_member() -> None:
    """族中混入其它设备或其它族的记录必须拒绝。"""
    token, first = _issued()
    rotated = rotate_device_refresh_credential(DEVICE, first, token, NOW, TTL_MS)
    foreign = replace(rotated.next, family_id="other-family")

    with pytest.raises(ValueError, match="其他设备或凭据族"):
        plan_refresh_credential_reuse_revocation(rotated.previous, (rotated.previous, foreign), NOW)


def test_reuse_revocation_rejects_family_without_credential() -> None:
    """族里必须包含被重用的那条记录。"""
    token, first = _issued()
    rotated = rotate_device_refresh_credential(DEVICE, first, token, NOW, TTL_MS)

    with pytest.raises(ValueError, match="不包含被重用的凭据"):
        plan_refresh_credential_reuse_revocation(rotated.previous, (rotated.next,), NOW)
