"""设备活动节流与撤销计划测试。

三条最要紧的性质：

1. **无需写入时返回原对象本身**——调用方靠对象同一性决定是否发起数据库写入；
   若返回等价副本，节流就形同虚设，每次认证都会写库。
2. **重复撤销保留首次撤销时间**——重放撤销请求不得改写审计时间。
3. **撤销计划严格限定同一设备**——否则一次撤销会波及别的设备。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lyra.security.device_auth import (
    DEFAULT_DEVICE_ACTIVITY_UPDATE_INTERVAL_MS,
    DeviceAccessTokenRecord,
    DeviceAuthError,
    DeviceRefreshCredentialRecord,
    PairedDeviceAuthRecord,
    PairedDeviceRecord,
    plan_device_access_token_revocation,
    plan_device_revocation,
    plan_paired_device_activity_update,
)

PAIRED_AT = datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC)

# schema 16 起设备必须归属一个用户与一条登录身份（``user_id`` / ``identity_id``
# 为 NOT NULL + 复合外键），构造记录时不能再省略。
USER_ID = "11111111-1111-1111-1111-111111111111"
IDENTITY_ID = "22222222-2222-2222-2222-222222222222"


def _device(
    *,
    last_seen_at: str | None = None,
    updated_at: str = "2026-10-01T00:00:00.000Z",
    status: str = "active",
    revoked_at: str | None = None,
) -> PairedDeviceRecord:
    """构造一台设备记录。"""
    return PairedDeviceRecord(
        id="device-1",
        user_id=USER_ID,
        identity_id=IDENTITY_ID,
        display_name="Pixel 9",
        public_key_pem="-----BEGIN PUBLIC KEY-----\nMIIB\n-----END PUBLIC KEY-----\n",
        public_key_fingerprint="f" * 64,
        key_algorithm="ec",
        status=status,  # type: ignore[arg-type]
        paired_at="2026-10-01T00:00:00.000Z",
        last_seen_at=last_seen_at,
        revoked_at=revoked_at,
        updated_at=updated_at,
    )


def _access_token(
    *, status: str = "active", device_id: str = "device-1", revoked_at: str | None = None
) -> DeviceAccessTokenRecord:
    """构造一条访问令牌记录。"""
    return DeviceAccessTokenRecord(
        id=f"token-{status}-{device_id}",
        device_id=device_id,
        token_hash="a" * 64,
        status=status,  # type: ignore[arg-type]
        expires_at="2026-10-13T00:00:00.000Z",
        last_seen_at=None,
        revoked_at=revoked_at,
        issued_at="2026-10-12T00:00:00.000Z",
    )


def _refresh(
    *,
    status: str = "active",
    device_id: str = "device-1",
    generation: int = 1,
    revoked_at: str | None = None,
) -> DeviceRefreshCredentialRecord:
    """构造一条刷新凭据记录。"""
    return DeviceRefreshCredentialRecord(
        id=f"cred-{generation}-{status}",
        device_id=device_id,
        family_id="family-1",
        generation=generation,
        token_hash="b" * 64,
        status=status,  # type: ignore[arg-type]
        expires_at="2026-11-12T00:00:00.000Z",
        last_used_at=None,
        rotated_at=None,
        revoked_at=revoked_at,
        created_at="2026-10-01T00:00:00.000Z",
    )


# --------------------------------------------------------------------------
# 设备活动节流
# --------------------------------------------------------------------------


def test_activity_update_writes_on_first_observation() -> None:
    """首次认证（无 last_seen_at）且晚于已知时间时必须产生新记录。"""
    device = _device()
    observed = PAIRED_AT + timedelta(days=1)

    updated = plan_paired_device_activity_update(device, observed)

    assert updated is not device
    assert updated.last_seen_at == "2026-10-02T00:00:00.000Z"
    assert updated.updated_at == "2026-10-02T00:00:00.000Z"
    # 其它字段保持不变。
    assert updated.public_key_fingerprint == device.public_key_fingerprint
    assert updated.paired_at == device.paired_at


def test_activity_update_skips_when_observation_not_newer() -> None:
    """观察时间不晚于已知时间时不产生写入，**且必须返回原对象**。"""
    device = _device(
        last_seen_at="2026-10-05T00:00:00.000Z", updated_at="2026-10-05T00:00:00.000Z"
    )

    same = plan_paired_device_activity_update(
        device, datetime(2026, 10, 5, 0, 0, 0, tzinfo=UTC)
    )
    older = plan_paired_device_activity_update(
        device, datetime(2026, 10, 4, 0, 0, 0, tzinfo=UTC)
    )

    assert same is device
    assert older is device


def test_activity_update_throttles_within_interval() -> None:
    """距上次记录不足最小间隔时不写入。"""
    device = _device(
        last_seen_at="2026-10-05T00:00:00.000Z", updated_at="2026-10-05T00:00:00.000Z"
    )
    just_within = datetime(2026, 10, 5, 0, 4, 59, tzinfo=UTC)

    assert plan_paired_device_activity_update(device, just_within) is device


def test_activity_update_writes_after_interval() -> None:
    """恰好达到最小间隔时必须写入。"""
    device = _device(
        last_seen_at="2026-10-05T00:00:00.000Z", updated_at="2026-10-05T00:00:00.000Z"
    )
    at_interval = datetime(2026, 10, 5, 0, 5, 0, tzinfo=UTC)

    updated = plan_paired_device_activity_update(device, at_interval)

    assert updated is not device
    assert updated.last_seen_at == "2026-10-05T00:05:00.000Z"


def test_activity_update_respects_custom_interval() -> None:
    """自定义间隔生效；0 表示每次都写。"""
    device = _device(
        last_seen_at="2026-10-05T00:00:00.000Z", updated_at="2026-10-05T00:00:00.000Z"
    )
    one_second_later = datetime(2026, 10, 5, 0, 0, 1, tzinfo=UTC)

    assert plan_paired_device_activity_update(device, one_second_later) is device
    assert plan_paired_device_activity_update(
        device, one_second_later, minimum_interval_ms=0
    ) is not device


def test_activity_update_default_interval_is_five_minutes() -> None:
    """默认间隔必须是 5 分钟——这是对外承诺的写入频率上限。"""
    assert DEFAULT_DEVICE_ACTIVITY_UPDATE_INTERVAL_MS == 5 * 60 * 1000


def test_activity_update_rejects_revoked_device() -> None:
    """已撤销设备不得再记录活动。"""
    device = _device(status="revoked", revoked_at="2026-10-02T00:00:00.000Z")
    with pytest.raises(DeviceAuthError) as raised:
        plan_paired_device_activity_update(device, PAIRED_AT + timedelta(days=1))
    assert raised.value.code == "DEVICE_REVOKED"


def test_activity_update_rejects_time_before_pairing() -> None:
    """早于配对时间的观察必须报错而不是静默忽略。"""
    with pytest.raises(ValueError, match="不能早于配对时间"):
        plan_paired_device_activity_update(_device(), PAIRED_AT - timedelta(seconds=1))


@pytest.mark.parametrize("interval", [-1, 1.5, True])
def test_activity_update_rejects_invalid_interval(interval: object) -> None:
    """间隔必须是非负整数毫秒数。"""
    with pytest.raises(ValueError, match="非负安全整数"):
        plan_paired_device_activity_update(
            _device(), PAIRED_AT + timedelta(days=1), minimum_interval_ms=interval  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# 访问令牌撤销
# --------------------------------------------------------------------------


def test_access_token_revocation_revokes_active_only() -> None:
    """只撤销 active 令牌；已撤销的保持原时间戳。"""
    now = datetime(2026, 10, 12, 0, 0, 0, tzinfo=UTC)
    already = _access_token(status="revoked", revoked_at="2026-10-01T00:00:00.000Z")
    active = _access_token(status="active")

    planned = plan_device_access_token_revocation("device-1", (active, already), now)

    assert planned[0].status == "revoked"
    assert planned[0].revoked_at == "2026-10-12T00:00:00.000Z"
    assert planned[1] is already


def test_access_token_revocation_rejects_foreign_device() -> None:
    """混入其它设备的令牌必须报错。"""
    now = datetime(2026, 10, 12, 0, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="其他设备"):
        plan_device_access_token_revocation(
            "device-1", (_access_token(status="active", device_id="device-2"),), now
        )


# --------------------------------------------------------------------------
# 设备撤销
# --------------------------------------------------------------------------


def test_device_revocation_covers_device_and_active_credentials() -> None:
    """撤销必须同时覆盖设备本身与其 active 刷新凭据。"""
    now = datetime(2026, 10, 12, 0, 0, 0, tzinfo=UTC)
    device = PairedDeviceAuthRecord(id="device-1", status="active", revoked_at=None)
    rotated = _refresh(status="rotated", generation=1)
    active = _refresh(status="active", generation=2)

    plan = plan_device_revocation(device, (rotated, active), now)

    assert plan.device.status == "revoked"
    assert plan.device.revoked_at == "2026-10-12T00:00:00.000Z"
    assert plan.credentials[0] is rotated
    assert plan.credentials[1].status == "revoked"
    assert plan.credentials[1].revoked_at == "2026-10-12T00:00:00.000Z"


def test_device_revocation_preserves_first_revocation_time() -> None:
    """重复撤销必须保留首次撤销时间，不改写审计记录。"""
    device = PairedDeviceAuthRecord(
        id="device-1", status="revoked", revoked_at="2026-10-01T00:00:00.000Z"
    )
    later = datetime(2026, 10, 20, 0, 0, 0, tzinfo=UTC)

    plan = plan_device_revocation(device, (_refresh(),), later)

    assert plan.device.revoked_at == "2026-10-01T00:00:00.000Z"
    # 凭据沿用同一时间戳，避免设备的撤销时间与凭据不一致。
    assert plan.credentials[0].revoked_at == "2026-10-01T00:00:00.000Z"


def test_device_revocation_rejects_foreign_credentials() -> None:
    """混入其它设备的刷新凭据必须报错。"""
    now = datetime(2026, 10, 12, 0, 0, 0, tzinfo=UTC)
    device = PairedDeviceAuthRecord(id="device-1", status="active", revoked_at=None)

    with pytest.raises(ValueError, match="其他设备的刷新凭据"):
        plan_device_revocation(device, (_refresh(device_id="device-2"),), now)


def test_device_revocation_is_idempotent_for_already_revoked_credentials() -> None:
    """已撤销的凭据保持原时间戳，重复调用幂等。"""
    now = datetime(2026, 10, 12, 0, 0, 0, tzinfo=UTC)
    device = PairedDeviceAuthRecord(id="device-1", status="active", revoked_at=None)
    already = _refresh(status="revoked", revoked_at="2026-09-01T00:00:00.000Z")

    plan = plan_device_revocation(device, (already,), now)

    assert plan.credentials[0] is already
