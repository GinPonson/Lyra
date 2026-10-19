"""幂等修改判定测试。

App 重试是常态，因此"同一个幂等键得到同一个结果"是写入路径的正确性前提。
本文件钉住五条分支与两处易漏的边界：

1. **重放不重复执行修改**——已完成记录直接返回结果引用；
2. **同键不同请求必须拒绝**（而不是静默重放别人的结果）；
3. **同键同请求仍在处理中可重试**——两种 409 的可重试性不同；
4. **过期记录可被替换**，且替换目标指向旧记录；
5. **明文幂等键永不入库**——记录里只有哈希。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.idempotent_mutation import (
    DEFAULT_IDEMPOTENCY_TTL_HOURS,
    IdempotentMutationReplay,
    IdempotentMutationStart,
    MutationIdempotencyRecord,
    complete_idempotent_mutation,
    hash_idempotency_key,
    plan_idempotent_mutation,
)

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
KEY = "idem-key-abcdef"
REQUEST_HASH = hashlib.sha256(b"request-body").hexdigest()
OTHER_REQUEST_HASH = hashlib.sha256(b"other-body").hexdigest()
DEVICE = "11111111-1111-1111-1111-111111111111"


def _record(
    *,
    status: str = "in_progress",
    request_hash: str = REQUEST_HASH,
    expires_at: str = "2026-10-13T09:00:00.000Z",
    result_resource_id: str | None = None,
    response_status_code: int | None = None,
    completed_at: str | None = None,
    device_id: str = DEVICE,
    operation: str = "import.commit",
    key: str = KEY,
) -> MutationIdempotencyRecord:
    """构造一条既有幂等记录。"""
    return MutationIdempotencyRecord(
        id="record-1", device_id=device_id, operation=operation,
        key_hash=hash_idempotency_key(key), request_hash=request_hash, status=status,
        result_resource_id=result_resource_id,
        response_status_code=response_status_code,
        created_at="2026-10-12T08:00:00.000Z",
        expires_at=expires_at,
        completed_at=completed_at,
    )


def _completed() -> MutationIdempotencyRecord:
    """构造一条已完成记录。"""
    return _record(
        status="completed", result_resource_id="job-42",
        response_status_code=202, completed_at="2026-10-12T08:30:00.000Z",
    )


def _plan(existing: MutationIdempotencyRecord | None = None, **overrides):
    """调用判定，默认参数可覆写。"""
    arguments = {
        "record_id": "record-new",
        "device_id": DEVICE,
        "operation": "import.commit",
        "key": KEY,
        "request_hash": REQUEST_HASH,
        "existing_record": existing,
        "now": NOW,
    }
    arguments.update(overrides)
    return plan_idempotent_mutation(**arguments)


# --------------------------------------------------------------------------
# 新建
# --------------------------------------------------------------------------


def test_starts_new_record_when_none_exists() -> None:
    """无既有记录时开始新记录。"""
    plan = _plan()

    assert isinstance(plan, IdempotentMutationStart)
    assert plan.replaces_expired_record_id is None
    assert plan.record.status == "in_progress"
    assert plan.record.created_at == "2026-10-12T09:00:00.000Z"
    assert plan.record.expires_at == "2026-10-13T09:00:00.000Z"
    assert plan.record.result_resource_id is None


def test_new_record_stores_only_the_key_hash() -> None:
    """记录里绝不能出现明文幂等键。"""
    plan = _plan()
    assert isinstance(plan, IdempotentMutationStart)

    assert plan.record.key_hash == hash_idempotency_key(KEY)
    assert KEY not in repr(plan.record)


def test_default_ttl_is_one_day() -> None:
    """默认保留 24 小时。"""
    assert DEFAULT_IDEMPOTENCY_TTL_HOURS == 24


def test_custom_ttl_is_applied() -> None:
    """自定义保留小时数生效。"""
    plan = _plan(ttl_hours=2)
    assert isinstance(plan, IdempotentMutationStart)
    assert plan.record.expires_at == "2026-10-12T11:00:00.000Z"


# --------------------------------------------------------------------------
# 重放
# --------------------------------------------------------------------------


def test_replays_completed_record_without_reexecuting() -> None:
    """已完成的同请求必须重放，且不改动记录。"""
    existing = _completed()
    plan = _plan(existing)

    assert isinstance(plan, IdempotentMutationReplay)
    assert plan.record_id == "record-1"
    assert plan.result_resource_id == "job-42"
    assert plan.response_status_code == 202
    assert plan.completed_at == "2026-10-12T08:30:00.000Z"


# --------------------------------------------------------------------------
# 拒绝路径
# --------------------------------------------------------------------------


def test_rejects_same_key_with_different_request() -> None:
    """同键不同请求必须拒绝，而不是重放别人的结果。"""
    with pytest.raises(ApplicationError) as raised:
        _plan(_record(request_hash=OTHER_REQUEST_HASH))

    assert raised.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert raised.value.status_code == 409
    assert raised.value.retryable is False


def test_in_progress_request_is_retryable() -> None:
    """同键同请求仍在处理中：409 但**可重试**。"""
    with pytest.raises(ApplicationError) as raised:
        _plan(_record(status="in_progress"))

    assert raised.value.code == "IDEMPOTENCY_REQUEST_IN_PROGRESS"
    assert raised.value.status_code == 409
    assert raised.value.retryable is True


def test_retryability_distinguishes_the_two_conflicts() -> None:
    """两种冲突的可重试性必须不同——混同会让客户端要么白等要么误弃。"""
    with pytest.raises(ApplicationError) as in_progress:
        _plan(_record(status="in_progress"))
    with pytest.raises(ApplicationError) as reused:
        _plan(_record(request_hash=OTHER_REQUEST_HASH))

    assert in_progress.value.retryable is not reused.value.retryable


def test_completed_record_missing_result_reference_is_an_error() -> None:
    """已完成却缺结果引用说明数据被破坏，必须报错而不是当成重放。"""
    broken = _record(status="completed")

    with pytest.raises(ValueError, match="缺少结果引用"):
        _plan(broken)


def test_foreign_record_is_rejected() -> None:
    """记录与查询条件不匹配说明索引已出错，必须报错。"""
    with pytest.raises(ValueError, match="不匹配"):
        _plan(_record(device_id="22222222-2222-2222-2222-222222222222"))


# --------------------------------------------------------------------------
# 过期替换
# --------------------------------------------------------------------------


def test_expired_record_is_replaced_not_replayed() -> None:
    """过期记录不再重放，而是被新记录原子替换。"""
    expired = _completed()
    expired = expired.__class__(**{
        **expired.__dict__, "expires_at": "2026-10-12T08:59:59.000Z"
    })

    plan = _plan(expired)

    assert isinstance(plan, IdempotentMutationStart)
    assert plan.replaces_expired_record_id == "record-1"


def test_expired_record_with_different_request_is_also_replaced() -> None:
    """过期后旧请求指纹不再有约束力——键可以在新请求上复用。"""
    expired = _record(
        request_hash=OTHER_REQUEST_HASH, expires_at="2026-10-12T08:59:59.000Z"
    )

    plan = _plan(expired)

    assert isinstance(plan, IdempotentMutationStart)
    assert plan.replaces_expired_record_id == "record-1"


def test_expiry_boundary_is_exclusive() -> None:
    """恰好到期视为已过期（复用键），早一毫秒视为未过期（重放）。"""
    at_expiry = _record(
        status="completed", result_resource_id="job-1", response_status_code=200,
        completed_at="2026-10-12T08:00:00.000Z",
        expires_at="2026-10-12T09:00:00.000Z",
    )
    assert isinstance(_plan(at_expiry), IdempotentMutationStart)

    still_valid = at_expiry.__class__(**{
        **at_expiry.__dict__, "expires_at": "2026-10-12T09:00:00.001Z"
    })
    assert isinstance(_plan(still_valid), IdempotentMutationReplay)


# --------------------------------------------------------------------------
# 字段校验
# --------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["", "  ", "x"])
def test_rejects_too_short_operation(operation: str) -> None:
    """操作名长度必须大于 1，与表的 CHECK 约束一致。"""
    with pytest.raises(ValueError):
        _plan(operation=operation)


@pytest.mark.parametrize("request_hash", ["", "abc", "A" * 64, "a" * 63])
def test_rejects_malformed_request_hash(request_hash: str) -> None:
    """请求指纹必须是 64 位小写十六进制。"""
    with pytest.raises(ValueError, match="64 位小写十六进制"):
        _plan(request_hash=request_hash)


@pytest.mark.parametrize("ttl", [0, -1, 1.5, True])
def test_rejects_invalid_ttl(ttl: object) -> None:
    """保留小时数必须是正整数。"""
    with pytest.raises(ValueError, match="正安全整数"):
        _plan(ttl_hours=ttl)  # type: ignore[arg-type]


def test_rejects_naive_datetime() -> None:
    """朴素时间必须拒绝：比较时会静默按本地时区解释。"""
    with pytest.raises(ValueError, match="必须带时区"):
        _plan(now=datetime(2026, 10, 12, 9, 0, 0))


# --------------------------------------------------------------------------
# 完成
# --------------------------------------------------------------------------


def test_complete_marks_record_finished() -> None:
    """完成写入资源引用、状态码与完成时间。"""
    plan = _plan()
    assert isinstance(plan, IdempotentMutationStart)

    completed = complete_idempotent_mutation(
        plan.record, result_resource_id="job-7", response_status_code=201, now=NOW
    )

    assert completed.status == "completed"
    assert completed.result_resource_id == "job-7"
    assert completed.response_status_code == 201
    assert completed.completed_at == "2026-10-12T09:00:00.000Z"
    # 键哈希与请求指纹保持不变。
    assert completed.key_hash == plan.record.key_hash


def test_complete_requires_in_progress() -> None:
    """已完成记录不能再次完成。"""
    with pytest.raises(ApplicationError) as raised:
        complete_idempotent_mutation(
            _completed(), result_resource_id="job-7", response_status_code=200, now=NOW
        )
    assert raised.value.code == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.parametrize("status_code", [199, 300, 0, 200.5, True])
def test_complete_rejects_non_2xx(status_code: object) -> None:
    """结果状态必须是 2xx 整数。"""
    plan = _plan()
    assert isinstance(plan, IdempotentMutationStart)

    with pytest.raises(ValueError, match="2xx"):
        complete_idempotent_mutation(
            plan.record, result_resource_id="job-7",
            response_status_code=status_code, now=NOW,  # type: ignore[arg-type]
        )


def test_completed_record_round_trips_through_replay() -> None:
    """完成后的记录再次提交同一请求，必须得到重放而不是新执行。"""
    plan = _plan()
    assert isinstance(plan, IdempotentMutationStart)
    completed = complete_idempotent_mutation(
        plan.record, result_resource_id="job-7", response_status_code=201, now=NOW
    )

    replay = _plan(completed, now=NOW + timedelta(minutes=5))

    assert isinstance(replay, IdempotentMutationReplay)
    assert replay.result_resource_id == "job-7"
    assert replay.response_status_code == 201
