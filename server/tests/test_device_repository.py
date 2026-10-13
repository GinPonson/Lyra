"""设备会话的 DuckDB 集成测试。

与 `test_device_pairing_service.py` 的内存储蓄不同，这里跑在**真实 schema 15**
上：验证 SQL 与表约束（UUID、TIMESTAMPTZ、CHECK、唯一索引）真正可用，而不是
只在内存假仓储上成立。

用真实库也顺带验证一件事：领域层按 **ISO 文本**比较时间，因此仓储必须往返
同形的文本，返回 ``datetime`` 会静默破坏过期判断。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.device_pairing_service import DevicePairingService
from lyra.application.write_coordinator import WriteCoordinator
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.security.device_auth import hash_opaque_credential

KEYS = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"]
PUBLIC_KEY_PEM = KEYS[0]["inputPem"]
OTHER_KEY_PEM = KEYS[1]["inputPem"]

GITHUB = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")
GOOGLE = LoginIdentity(
    provider="google", subject="112233", email="owner@example.com", display_name="Owner"
)


@pytest.fixture()
def wired(tmp_path: Path):
    """在临时库上装配真实仓储与服务。"""
    database = open_lyra_database(str(tmp_path / "devices.duckdb"))
    repository = DuckDbDeviceRepository(database.connection)
    service = DevicePairingService(repository, WriteCoordinator())
    try:
        yield service, database.connection
    finally:
        database.close()


def _authorize(service: DevicePairingService, pem: str = PUBLIC_KEY_PEM, name: str = "Pixel 9"):
    """签发设备凭据。"""
    return asyncio.run(
        service.authorize_owner_device(
            identity=GITHUB, display_name=name, public_key_pem=pem
        )
    )


def _count(connection, table: str) -> int:
    """统计表行数。"""
    row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_authorize_persists_device_and_credentials(wired) -> None:
    """签发必须把设备与两类凭据真正写进 schema 15。"""
    service, connection = wired

    response = _authorize(service)

    assert _count(connection, "paired_devices") == 1
    assert _count(connection, "device_refresh_credentials") == 1
    assert _count(connection, "device_access_tokens") == 1

    row = connection.execute(
        "SELECT status, display_name, revoked_at FROM paired_devices"
    ).fetchone()
    assert row is not None
    assert row[0] == "active"
    assert row[1] == "Pixel 9"
    assert row[2] is None
    assert response.access_token_expires_at.endswith("Z")


def test_timestamps_round_trip_as_iso_text(wired) -> None:
    """仓储必须返回 ISO 文本；返回 datetime 会让超时判断静默失效。"""
    service, connection = wired
    issued = _authorize(service)

    repository = DuckDbDeviceRepository(connection)
    record = repository.load_refresh_credential(hash_opaque_credential(issued.refresh_token))

    assert record is not None
    assert isinstance(record.expires_at, str)
    assert record.expires_at.endswith("Z")
    # 文本可直接被领域层解析，因此刷新时的过期比较成立。
    datetime.fromisoformat(record.expires_at.replace("Z", "+00:00"))


def test_repair_reuses_device_and_revokes_previous_credentials(wired) -> None:
    """同一公钥重新配对：复用设备行并撤销旧凭据。"""
    service, connection = wired
    first = _authorize(service)
    old_hash = hash_opaque_credential(first.refresh_token)

    second = _authorize(service, name="Pixel 9 again")

    assert second.device_id == first.device_id
    assert _count(connection, "paired_devices") == 1
    row = connection.execute(
        "SELECT status FROM device_refresh_credentials WHERE token_hash = ?", [old_hash]
    ).fetchone()
    assert row is not None and row[0] == "revoked"
    # 新设备记录被恢复为 active 且清空撤销时间。
    device = connection.execute("SELECT status, revoked_at FROM paired_devices").fetchone()
    assert device is not None and device[0] == "active" and device[1] is None


def test_distinct_keys_create_distinct_devices(wired) -> None:
    """不同公钥对应不同设备行（指纹列有唯一约束）。"""
    service, connection = wired
    _authorize(service)
    _authorize(service, pem=OTHER_KEY_PEM, name="Tablet")

    assert _count(connection, "paired_devices") == 2


def test_owner_identity_recorded_per_provider(wired) -> None:
    """每个来源各存一行 owner 身份，邮箱/昵称不跨来源关联。"""
    service, connection = wired

    asyncio.run(service.authorize_owner_device(
        identity=GITHUB, display_name="Pixel 9", public_key_pem=PUBLIC_KEY_PEM
    ))
    asyncio.run(service.authorize_owner_device(
        identity=GOOGLE, display_name="Tablet", public_key_pem=OTHER_KEY_PEM
    ))

    rows = connection.execute(
        "SELECT provider, subject, created_at FROM server_owner_identity ORDER BY provider"
    ).fetchall()
    assert [row[0] for row in rows] == ["github", "google"]
    assert [row[1] for row in rows] == ["12345", "112233"]
    assert all(isinstance(row[2], datetime) for row in rows)


def test_refresh_rotates_in_database(wired) -> None:
    """刷新必须把旧代标为 rotated 并插入新代与新的访问令牌。"""
    service, connection = wired
    issued = _authorize(service)

    refreshed = asyncio.run(service.refresh_tokens(refresh_token=issued.refresh_token))

    assert _count(connection, "device_refresh_credentials") == 2
    assert _count(connection, "device_access_tokens") == 2
    rows = dict(connection.execute(
        "SELECT generation, status FROM device_refresh_credentials ORDER BY generation"
    ).fetchall())
    assert rows == {1: "rotated", 2: "active"}
    assert refreshed.refresh_token != issued.refresh_token


def test_reuse_revokes_whole_family_in_database(wired) -> None:
    """重用检测必须把整族写为 revoked，包括活动的最新代。"""
    service, connection = wired
    issued = _authorize(service)
    asyncio.run(service.refresh_tokens(refresh_token=issued.refresh_token))

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(service.refresh_tokens(refresh_token=issued.refresh_token))
    assert raised.value.code == "DEVICE_CREDENTIAL_REUSED"

    statuses = {row[0] for row in connection.execute(
        "SELECT DISTINCT status FROM device_refresh_credentials"
    ).fetchall()}
    assert statuses == {"revoked"}


def test_revoke_device_marks_rows_in_database(wired) -> None:
    """退出登录必须把设备、刷新凭据与访问令牌都标记为 revoked。"""
    service, connection = wired
    issued = _authorize(service)

    asyncio.run(service.revoke_device(issued.device_id))

    device = connection.execute("SELECT status, revoked_at FROM paired_devices").fetchone()
    assert device is not None and device[0] == "revoked" and device[1] is not None
    assert {row[0] for row in connection.execute(
        "SELECT DISTINCT status FROM device_refresh_credentials"
    ).fetchall()} == {"revoked"}
    assert {row[0] for row in connection.execute(
        "SELECT DISTINCT status FROM device_access_tokens"
    ).fetchall()} == {"revoked"}


def test_revoke_is_idempotent_in_database(wired) -> None:
    """重复撤销保留首次撤销时间（``coalesce``）。"""
    service, connection = wired
    issued = _authorize(service)
    asyncio.run(service.revoke_device(issued.device_id))
    first = connection.execute("SELECT revoked_at FROM paired_devices").fetchone()

    asyncio.run(service.revoke_device(issued.device_id))
    second = connection.execute("SELECT revoked_at FROM paired_devices").fetchone()

    assert first is not None and second is not None
    assert first[0] == second[0]


def test_rollback_leaves_no_partial_credentials(wired) -> None:
    """事务失败必须整体回滚，不留下半提交的凭据。"""
    service, connection = wired
    issued = _authorize(service)
    before = _count(connection, "device_refresh_credentials")

    class Exploding(DuckDbDeviceRepository):
        """在插入访问令牌时失败，用于验证回滚。"""

        def create_access_token(self, record):  # type: ignore[no-untyped-def]
            raise RuntimeError("injected failure")

    repository = Exploding(connection)
    failing = DevicePairingService(repository, WriteCoordinator())

    with pytest.raises(RuntimeError, match="injected failure"):
        asyncio.run(failing.authorize_owner_device(
            identity=GOOGLE, display_name="Tablet", public_key_pem=OTHER_KEY_PEM
        ))

    # owner 身份与设备插入都被同一事务回滚。
    assert _count(connection, "device_refresh_credentials") == before
    assert _count(connection, "paired_devices") == 1
    assert connection.execute(
        "SELECT count(*) FROM server_owner_identity WHERE provider = 'google'"
    ).fetchone()[0] == 0


def test_clock_timezone_is_utc(wired) -> None:
    """签发写入的时间戳必须是 UTC，否则设备活动比较会出现时区偏移。"""
    service, connection = wired
    _authorize(service)

    row = connection.execute("SELECT paired_at FROM paired_devices").fetchone()
    assert row is not None
    paired_at = row[0]
    assert isinstance(paired_at, datetime)
    offset = paired_at.astimezone(UTC).utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0
