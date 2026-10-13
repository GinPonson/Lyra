"""设备会话服务测试（签发 / 刷新 / 撤销）。

用内存仓储替换数据库，因此测试是确定性的；服务层的事务边界、TTL 与安全语义
保持不变。重点覆盖：

1. **同一公钥指纹复用设备 ID**——重装 App 后用同一把密钥登录，仍是同一台设备；
2. **重新配对先撤销旧凭据**——否则旧 refresh 在新的会话建立后仍可用；
3. **重用检测撤销整个凭据族**——只拒绝这一次请求等于把窗口留给攻击者；
4. **撤销幂等**——重放退出登录不改写审计时间。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.device_pairing_service import (
    ACCESS_TOKEN_TTL_MILLISECONDS,
    REFRESH_TOKEN_TTL_MILLISECONDS,
    DevicePairingService,
)
from lyra.application.write_coordinator import WriteCoordinator
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.security.device_auth import (
    DeviceAccessTokenRecord,
    DeviceRefreshCredentialRecord,
    PairedDeviceAuthRecord,
    PairedDeviceRecord,
    hash_opaque_credential,
)

PUBLIC_KEY_PEM = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"][0]["inputPem"]
OTHER_KEY_PEM = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"][1]["inputPem"]

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")


class FakeRepository:
    """内存仓储：与协议一一对应，并记录调用顺序。"""

    def __init__(self) -> None:
        self.devices: dict[str, PairedDeviceRecord] = {}
        self.refresh: dict[str, DeviceRefreshCredentialRecord] = {}
        self.access: dict[str, DeviceAccessTokenRecord] = {}
        self.owner_identity: LoginIdentity | None = None
        self.transactions = 0
        self.calls: list[str] = []

    @contextmanager
    def transaction(self):
        """最简事务：记录次数；异常时不做部分提交（内存实现天然如此）。"""
        self.transactions += 1
        self.calls.append("begin")
        try:
            yield
        except Exception:
            self.calls.append("rollback")
            raise
        self.calls.append("commit")

    def claim_server_owner_identity(self, identity: LoginIdentity, now: datetime) -> None:
        self.owner_identity = identity
        self.calls.append("claim_owner")

    def find_device_id_by_public_key(self, fingerprint: str) -> str | None:
        self.calls.append("find_device")
        for device_id, record in self.devices.items():
            if record.public_key_fingerprint == fingerprint:
                return device_id
        return None

    def create_paired_device(self, record: PairedDeviceRecord) -> None:
        self.calls.append("create_device")
        self.devices[record.id] = record

    def reactivate_paired_device(self, device_id: str, display_name: str, now: datetime) -> None:
        self.calls.append("reactivate_device")
        device = self.devices[device_id]
        self.devices[device_id] = PairedDeviceRecord(
            id=device.id,
            display_name=display_name,
            public_key_pem=device.public_key_pem,
            public_key_fingerprint=device.public_key_fingerprint,
            key_algorithm=device.key_algorithm,
            status="active",
            paired_at=device.paired_at,
            last_seen_at=device.last_seen_at,
            revoked_at=None,
            updated_at=device.updated_at,
        )

    def revoke_device_credentials(self, device_id: str, now: datetime) -> None:
        self.calls.append("revoke_credentials")
        stamp = now.isoformat().replace("+00:00", "Z")
        for token_hash, record in list(self.refresh.items()):
            if record.device_id == device_id and record.status == "active":
                self.refresh[token_hash] = _replace(record, status="revoked", revoked_at=stamp)
        for token_id, record in list(self.access.items()):
            if record.device_id == device_id and record.status == "active":
                self.access[token_id] = _replace(record, status="revoked", revoked_at=stamp)

    def create_refresh_credential(self, record: DeviceRefreshCredentialRecord) -> None:
        self.calls.append("create_refresh")
        self.refresh[record.token_hash] = record

    def create_access_token(self, record: DeviceAccessTokenRecord) -> None:
        self.calls.append("create_access")
        self.access[record.id] = record

    def load_refresh_credential(self, token_hash: str) -> DeviceRefreshCredentialRecord | None:
        self.calls.append("load_refresh")
        return self.refresh.get(token_hash)

    def load_paired_device_auth(self, device_id: str) -> PairedDeviceAuthRecord | None:
        self.calls.append("load_device")
        device = self.devices.get(device_id)
        if device is None:
            return None
        return PairedDeviceAuthRecord(
            id=device.id, status=device.status, revoked_at=device.revoked_at
        )

    def load_refresh_credential_family(
        self, device_id: str, family_id: str
    ) -> tuple[DeviceRefreshCredentialRecord, ...]:
        self.calls.append("load_family")
        return tuple(
            record
            for record in self.refresh.values()
            if record.device_id == device_id and record.family_id == family_id
        )

    def rotate_refresh_credential(self, rotation: Any, access_record: DeviceAccessTokenRecord) -> None:
        self.calls.append("rotate")
        self.refresh[rotation.previous.token_hash] = rotation.previous
        self.refresh[rotation.next.token_hash] = rotation.next
        self.access[access_record.id] = access_record

    def apply_family_revocation(self, plan: Any) -> None:
        self.calls.append("apply_revocation")
        for credential in plan.credentials:
            self.refresh[credential.token_hash] = credential

    def mark_device_revoked(self, device_id: str, now: datetime) -> None:
        self.calls.append("mark_device_revoked")
        device = self.devices[device_id]
        stamp = device.revoked_at or now.isoformat().replace("+00:00", "Z")
        self.devices[device_id] = _replace(
            device, status="revoked", revoked_at=stamp
        )


def _replace(record: Any, **changes: Any) -> Any:
    """dataclass 的 replace 包装；保持调用点简洁。"""
    from dataclasses import replace

    return replace(record, **changes)


def _service(now: datetime = NOW) -> tuple[DevicePairingService, FakeRepository]:
    """构造服务与内存仓储。"""
    repository = FakeRepository()
    service = DevicePairingService(repository, WriteCoordinator(), now=lambda: now)
    return service, repository


def _authorize(service: DevicePairingService, pem: str = PUBLIC_KEY_PEM, name: str = "Pixel 9"):
    """签发设备凭据。"""
    return asyncio.run(
        service.authorize_owner_device(
            identity=IDENTITY, display_name=name, public_key_pem=pem
        )
    )


# --------------------------------------------------------------------------
# 签发
# --------------------------------------------------------------------------


def test_authorize_issues_device_and_both_credentials() -> None:
    """签发应返回设备 ID、名称与一对凭据，并记录 owner 身份。"""
    service, repository = _service()

    response = _authorize(service)

    assert response.device_id
    assert response.display_name == "Pixel 9"
    assert response.access_token
    assert response.refresh_token
    assert repository.owner_identity == IDENTITY
    assert len(repository.refresh) == 1
    assert len(repository.access) == 1


def test_authorize_uses_expected_ttls() -> None:
    """访问令牌 15 分钟、刷新凭据 30 天。"""
    service, _ = _service()

    response = _authorize(service)

    assert response.access_token_expires_at == "2026-10-12T09:15:00.000Z"
    assert response.refresh_token_expires_at == "2026-11-11T09:00:00.000Z"
    assert ACCESS_TOKEN_TTL_MILLISECONDS == 15 * 60 * 1000
    assert REFRESH_TOKEN_TTL_MILLISECONDS == 30 * 24 * 60 * 60 * 1000


def test_authorize_reuses_device_id_for_same_public_key() -> None:
    """同一公钥重新配对必须复用设备 ID，而不是新增一台设备。"""
    service, repository = _service()
    first = _authorize(service)
    second = _authorize(service, name="Pixel 9 (reinstalled)")

    assert second.device_id == first.device_id
    assert second.display_name == "Pixel 9 (reinstalled)"
    assert len(repository.devices) == 1


def test_authorize_revokes_previous_credentials_on_repair() -> None:
    """重新配对必须先撤销旧凭据，否则旧 refresh 在新会话建立后仍可用。"""
    service, repository = _service()
    first = _authorize(service)
    old_hash = hash_opaque_credential(first.refresh_token)

    _authorize(service)

    assert repository.refresh[old_hash].status == "revoked"
    # 只看第二次授权那一轮：`.index` 返回首次出现的位置，跨轮比较会得出错误结论。
    second_round = repository.calls[repository.calls.index("commit") + 1 :]
    assert second_round.index("revoke_credentials") < second_round.index("create_refresh")


def test_authorize_creates_new_device_for_different_key() -> None:
    """不同公钥对应不同设备。"""
    service, repository = _service()
    first = _authorize(service)
    second = _authorize(service, pem=OTHER_KEY_PEM)

    assert second.device_id != first.device_id
    assert len(repository.devices) == 2


def test_authorize_rejects_invalid_public_key() -> None:
    """非法公钥必须在事务开始前拒绝。"""
    service, repository = _service()

    with pytest.raises(Exception, match="公钥"):
        _authorize(service, pem="not a key")
    assert repository.transactions == 0


def test_authorize_is_atomic() -> None:
    """一次签发恰好使用一个事务。"""
    service, repository = _service()
    _authorize(service)

    assert repository.transactions == 1
    assert repository.calls[-1] == "commit"


# --------------------------------------------------------------------------
# 刷新
# --------------------------------------------------------------------------


def test_refresh_rotates_and_issues_new_access_token() -> None:
    """刷新应轮换刷新凭据并签发新的访问令牌。"""
    service, repository = _service()
    issued = _authorize(service)

    refreshed = asyncio.run(service.refresh_tokens(refresh_token=issued.refresh_token))

    assert refreshed.refresh_token != issued.refresh_token
    assert refreshed.access_token != issued.access_token
    assert repository.refresh[hash_opaque_credential(issued.refresh_token)].status == "rotated"
    assert repository.refresh[hash_opaque_credential(refreshed.refresh_token)].status == "active"
    assert repository.refresh[hash_opaque_credential(refreshed.refresh_token)].generation == 2


def test_refresh_rejects_unknown_token() -> None:
    """未知刷新凭据统一报「凭据无效」，不区分不存在与不匹配。"""
    service, _ = _service()
    with pytest.raises(ApplicationError) as raised:
        asyncio.run(service.refresh_tokens(refresh_token="unknown-token"))
    assert raised.value.code == "DEVICE_CREDENTIAL_INVALID"
    assert raised.value.status_code == 401


def test_refresh_rejects_missing_device() -> None:
    """令牌存在但设备记录缺失时同样报无效。"""
    service, repository = _service()
    issued = _authorize(service)
    repository.devices.clear()

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(service.refresh_tokens(refresh_token=issued.refresh_token))
    assert raised.value.code == "DEVICE_CREDENTIAL_INVALID"


def test_refresh_detects_reuse_and_revokes_whole_family() -> None:
    """重复提交旧刷新凭据必须撤销整族，包括活动的最新代。"""
    service, repository = _service()
    issued = _authorize(service)
    refreshed = asyncio.run(service.refresh_tokens(refresh_token=issued.refresh_token))

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(service.refresh_tokens(refresh_token=issued.refresh_token))
    assert raised.value.code == "DEVICE_CREDENTIAL_REUSED"

    # 活动的最新代也被撤销，攻击者无法继续刷新。
    latest = repository.refresh[hash_opaque_credential(refreshed.refresh_token)]
    assert latest.status == "revoked"


def test_refresh_after_logout_reports_reuse_not_revocation() -> None:
    """登出后刷新报「重用」并撤销整族，而不是「设备已撤销」。

    这不是措辞问题：退出登录会撤销该设备全部凭据，因此**非 active 一律按重放
    处理**。把已撤销的凭据当作可重试的会话错误，会让攻击者在拿到旧令牌后
    仍有机会试探。现有实现同样如此，迁移保持该行为。
    """
    service, repository = _service()
    issued = _authorize(service)
    asyncio.run(service.revoke_device(issued.device_id))

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(service.refresh_tokens(refresh_token=issued.refresh_token))

    assert raised.value.code == "DEVICE_CREDENTIAL_REUSED"
    assert raised.value.status_code == 401
    # 整族被撤销后，所有凭据都不再可用。
    assert all(record.status == "revoked" for record in repository.refresh.values())


def test_refresh_rejects_expired_credential() -> None:
    """过期的刷新凭据必须报过期。"""
    service, repository = _service()
    issued = _authorize(service)
    record = repository.refresh[hash_opaque_credential(issued.refresh_token)]
    repository.refresh[hash_opaque_credential(issued.refresh_token)] = _replace(
        record, expires_at="2026-10-01T00:00:00.000Z"
    )

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(service.refresh_tokens(refresh_token=issued.refresh_token))
    assert raised.value.code == "DEVICE_CREDENTIAL_EXPIRED"


def test_refresh_uses_single_transaction() -> None:
    """一次刷新在轮换时恰好使用一个事务。"""
    service, repository = _service()
    issued = _authorize(service)
    before = repository.transactions

    asyncio.run(service.refresh_tokens(refresh_token=issued.refresh_token))

    assert repository.transactions == before + 1


# --------------------------------------------------------------------------
# 撤销
# --------------------------------------------------------------------------


def test_revoke_device_marks_device_and_credentials() -> None:
    """退出登录必须撤销设备及其全部凭据。"""
    service, repository = _service()
    issued = _authorize(service)

    asyncio.run(service.revoke_device(issued.device_id))

    assert repository.devices[issued.device_id].status == "revoked"
    assert repository.devices[issued.device_id].revoked_at is not None
    assert all(record.status == "revoked" for record in repository.refresh.values())
    assert all(record.status == "revoked" for record in repository.access.values())


def test_revoke_device_preserves_first_revocation_time() -> None:
    """重复撤销保留首次撤销时间，不改写审计记录。"""
    service, repository = _service()
    issued = _authorize(service)
    asyncio.run(service.revoke_device(issued.device_id))
    first = repository.devices[issued.device_id].revoked_at

    asyncio.run(service.revoke_device(issued.device_id))

    assert repository.devices[issued.device_id].revoked_at == first


def test_reauthorization_after_revoke_reactivates_same_device() -> None:
    """退出后重新登录应复用同一设备 ID 并恢复 active。"""
    service, repository = _service()
    issued = _authorize(service)
    asyncio.run(service.revoke_device(issued.device_id))

    again = _authorize(service)

    assert again.device_id == issued.device_id
    assert repository.devices[issued.device_id].status == "active"
    assert repository.devices[issued.device_id].revoked_at is None


# --------------------------------------------------------------------------
# 单写队列
# --------------------------------------------------------------------------


def test_write_coordinator_serializes_tasks() -> None:
    """并发入队的写任务必须串行执行，不能交错。"""
    coordinator = WriteCoordinator()
    active = 0
    max_active = 0
    order: list[int] = []

    async def task(index: int) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.005)
        order.append(index)
        active -= 1

    async def run() -> None:
        await asyncio.gather(*(coordinator.enqueue(lambda i=i: task(i)) for i in range(5)))

    asyncio.run(run())

    assert max_active == 1
    assert order == [0, 1, 2, 3, 4]


def test_write_coordinator_rejects_after_close() -> None:
    """关闭后拒绝新任务。"""
    coordinator = WriteCoordinator()
    asyncio.run(coordinator.close())

    async def run() -> None:
        with pytest.raises(RuntimeError, match="正在关闭"):
            await coordinator.enqueue(lambda: asyncio.sleep(0))

    asyncio.run(run())


def test_write_coordinator_continues_after_failure() -> None:
    """一个任务失败不得污染队列。"""
    coordinator = WriteCoordinator()
    done: list[int] = []

    async def failing() -> None:
        raise RuntimeError("boom")

    async def ok() -> None:
        done.append(1)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="boom"):
            await coordinator.enqueue(failing)
        await coordinator.enqueue(ok)

    asyncio.run(run())

    assert done == [1]
    assert coordinator.pending_operations == 0
