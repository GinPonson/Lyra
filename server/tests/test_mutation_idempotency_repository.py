"""设备级幂等记录仓储测试。

三条状态纪律，各自防一种错误：

| 纪律 | 不遵守会怎样 |
| --- | --- |
| **完成以 ``status='in_progress'`` 做 CAS** | 两个并发请求都"完成"同一条，后写的覆盖先写的 |
| **释放只删 ``in_progress``** | 已完成的记录被删掉，**重放会真的再执行一次** |
| **过期记录先删再插** | 新请求撞 UNIQUE，用户被一个已过期的键永久挡住 |
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from lyra.db.connection import open_lyra_database
from lyra.infrastructure.db.mutation_idempotency_repository import (
    complete_mutation_idempotency_record,
    load_mutation_idempotency_record,
    new_idempotency_record,
    release_mutation_idempotency_record,
    start_mutation_idempotency_record,
)

NOW = datetime(2026, 10, 13, 9, 0, 0, tzinfo=UTC)
DEVICE_ID = "11111111-1111-1111-1111-111111111111"
KEY_HASH = "a" * 64
REQUEST_HASH = "b" * 64
OPERATION = "import.commit"


@pytest.fixture()
def database(tmp_path: Path):
    """打开一个空库并登记一台设备。"""
    handle = open_lyra_database(str(tmp_path / "idem.duckdb"))
    _insert_device(handle.connection, DEVICE_ID)
    try:
        yield handle
    finally:
        handle.close()


def _insert_device(connection, device_id: str) -> None:
    """登记一台设备（列名取自真实 schema）。

    **指纹按设备 ID 派生**：schema 对 ``public_key_fingerprint`` 有唯一约束，
    两台设备用同一个指纹会撞约束。
    """
    fingerprint = device_id.replace("-", "").ljust(64, "0")[:64]
    connection.execute(
        """
        INSERT INTO paired_devices
            (id, display_name, public_key_pem, public_key_fingerprint, key_algorithm, status)
        VALUES (CAST(? AS UUID), 'Pixel', '-----BEGIN PUBLIC KEY-----', ?, 'ec', 'active')
        """,
        [device_id, fingerprint],
    )


def _record(**overrides):
    """构造一条待登记记录。"""
    base = {
        "device_id": DEVICE_ID, "operation": OPERATION, "key_hash": KEY_HASH,
        "request_hash": REQUEST_HASH, "created_at": NOW, "ttl_seconds": 3600,
    }
    base.update(overrides)
    return new_idempotency_record(**base)


def _start(database, record=None, **kwargs):
    """登记一条记录。"""
    record = record or _record()
    start_mutation_idempotency_record(database.connection, record=record, **kwargs)
    return record


# --------------------------------------------------------------------------
# 构造
# --------------------------------------------------------------------------


def test_new_record_is_in_progress() -> None:
    """新记录状态为 in_progress，结果字段为空。"""
    record = _record()

    assert record.status == "in_progress"
    assert record.result_resource_id is None
    assert record.response_status_code is None
    assert record.completed_at is None
    assert record.created_at == "2026-10-13T09:00:00.000Z"
    assert record.expires_at == "2026-10-13T10:00:00.000Z"


def test_new_record_gets_a_fresh_id() -> None:
    """每次构造都得到新 ID。"""
    assert _record().id != _record().id


@pytest.mark.parametrize("ttl", [0, -1])
def test_non_positive_ttl_is_rejected(ttl: int) -> None:
    """**TTL 必须为正**：否则 ``expires_at`` 会早于 ``created_at``，撞 schema 的 CHECK。"""
    with pytest.raises(ValueError, match="正秒数"):
        _record(ttl_seconds=ttl)


def test_naive_created_at_is_rejected() -> None:
    """创建时间必须带时区。"""
    with pytest.raises(ValueError, match="必须带时区"):
        _record(created_at=datetime(2026, 10, 13, 9, 0, 0))


# --------------------------------------------------------------------------
# 登记与读取
# --------------------------------------------------------------------------


def test_start_then_load_round_trips(database) -> None:
    """登记后可读回。"""
    record = _start(database)

    loaded = load_mutation_idempotency_record(
        database.connection, device_id=DEVICE_ID, operation=OPERATION, key_hash=KEY_HASH
    )

    assert loaded is not None
    assert loaded.id == record.id
    assert loaded.status == "in_progress"
    assert loaded.request_hash == REQUEST_HASH


def test_unknown_key_returns_none(database) -> None:
    """未登记过的键返回 None。"""
    assert load_mutation_idempotency_record(
        database.connection, device_id=DEVICE_ID, operation=OPERATION,
        key_hash="c" * 64,
    ) is None


def test_the_same_key_cannot_be_started_twice(database) -> None:
    """**同一（设备、操作、键）只能有一条记录**——这正是幂等键的机制。"""
    _start(database)

    with pytest.raises(duckdb.ConstraintException):
        _start(database, _record())


def test_different_devices_may_use_the_same_key(database) -> None:
    """不同设备的相同键互不干扰。"""
    other = str(uuid.uuid4())
    _insert_device(database.connection, other)
    _start(database)
    _start(database, _record(device_id=other))

    assert load_mutation_idempotency_record(
        database.connection, device_id=other, operation=OPERATION, key_hash=KEY_HASH
    ) is not None


def test_expired_record_can_be_replaced(database) -> None:
    """**过期记录先删再插**，否则新请求撞上 UNIQUE 约束。

    那会让用户被一个**已过期的键永久挡住**，而重试永远不成功。
    """
    old = _start(database)
    replacement = _record()

    start_mutation_idempotency_record(
        database.connection, record=replacement, replaces_expired_record_id=old.id
    )

    loaded = load_mutation_idempotency_record(
        database.connection, device_id=DEVICE_ID, operation=OPERATION, key_hash=KEY_HASH
    )
    assert loaded is not None and loaded.id == replacement.id


# --------------------------------------------------------------------------
# 完成（CAS）
# --------------------------------------------------------------------------


def test_completion_records_the_result(database) -> None:
    """完成后写入结果、状态码与完成时间。"""
    record = _start(database)

    complete_mutation_idempotency_record(
        database.connection, record_id=record.id,
        result_resource_id="resource-1", response_status_code=201,
        completed_at=NOW + timedelta(seconds=5),
    )

    loaded = load_mutation_idempotency_record(
        database.connection, device_id=DEVICE_ID, operation=OPERATION, key_hash=KEY_HASH
    )
    assert loaded is not None
    assert loaded.status == "completed"
    assert loaded.result_resource_id == "resource-1"
    assert loaded.response_status_code == 201
    assert loaded.completed_at == "2026-10-13T09:00:05.000Z"


def test_double_completion_is_rejected(database) -> None:
    """**完成以 ``in_progress`` 做 CAS。**

    两个并发请求都"完成"同一条记录时，只有一方能写入；后到的一方收到错误而不是
    **静默覆盖先写的那个结果**——否则客户端可能拿到一个指向错误资源的 ID。
    """
    record = _start(database)
    complete_mutation_idempotency_record(
        database.connection, record_id=record.id,
        result_resource_id="first", response_status_code=200,
        completed_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="完成收口失败"):
        complete_mutation_idempotency_record(
            database.connection, record_id=record.id,
            result_resource_id="second", response_status_code=200,
            completed_at=NOW + timedelta(seconds=2),
        )

    loaded = load_mutation_idempotency_record(
        database.connection, device_id=DEVICE_ID, operation=OPERATION, key_hash=KEY_HASH
    )
    assert loaded is not None and loaded.result_resource_id == "first"


def test_completing_an_unknown_record_is_rejected(database) -> None:
    """未知记录 ID 同样报错，不静默成功。"""
    with pytest.raises(ValueError, match="完成收口失败"):
        complete_mutation_idempotency_record(
            database.connection, record_id=str(uuid.uuid4()),
            result_resource_id="x", response_status_code=200, completed_at=NOW,
        )


def test_completion_requires_a_timezone(database) -> None:
    """完成时间必须带时区。"""
    record = _start(database)

    with pytest.raises(ValueError, match="必须带时区"):
        complete_mutation_idempotency_record(
            database.connection, record_id=record.id,
            result_resource_id="x", response_status_code=200,
            completed_at=datetime(2026, 10, 13, 9, 0, 0),
        )


def test_non_2xx_completion_violates_the_schema_check(database) -> None:
    """**已完成的记录必须是 2xx。**

    schema 把状态与状态码绑在一起，因此"完成了一次失败"这种记录不可能存在。
    """
    record = _start(database)

    with pytest.raises(duckdb.ConstraintException):
        complete_mutation_idempotency_record(
            database.connection, record_id=record.id,
            result_resource_id="x", response_status_code=500, completed_at=NOW,
        )


def test_empty_result_resource_violates_the_schema_check(database) -> None:
    """已完成必须有非空结果 ID。"""
    record = _start(database)

    with pytest.raises(duckdb.ConstraintException):
        complete_mutation_idempotency_record(
            database.connection, record_id=record.id,
            result_resource_id="   ", response_status_code=200, completed_at=NOW,
        )


# --------------------------------------------------------------------------
# 释放
# --------------------------------------------------------------------------


def test_release_removes_an_in_progress_record(database) -> None:
    """失败回滚时释放 in_progress 记录，键可再次使用。"""
    record = _start(database)

    release_mutation_idempotency_record(database.connection, record.id)

    assert load_mutation_idempotency_record(
        database.connection, device_id=DEVICE_ID, operation=OPERATION, key_hash=KEY_HASH
    ) is None


def test_release_does_not_remove_a_completed_record(database) -> None:
    """**释放只删 ``in_progress``。**

    已完成的记录被删掉会让幂等保证消失——**重放会真的再执行一次**，
    而那正是幂等键要防的事。
    """
    record = _start(database)
    complete_mutation_idempotency_record(
        database.connection, record_id=record.id,
        result_resource_id="resource-1", response_status_code=201, completed_at=NOW,
    )

    release_mutation_idempotency_record(database.connection, record.id)

    loaded = load_mutation_idempotency_record(
        database.connection, device_id=DEVICE_ID, operation=OPERATION, key_hash=KEY_HASH
    )
    assert loaded is not None
    assert loaded.status == "completed"
    assert loaded.result_resource_id == "resource-1"


def test_release_of_an_unknown_record_is_silent(database) -> None:
    """未知记录 ID 静默返回——释放是尽力而为的清理动作。"""
    release_mutation_idempotency_record(database.connection, str(uuid.uuid4()))
