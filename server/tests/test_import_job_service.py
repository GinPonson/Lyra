"""导入作业生命周期编排测试。

重点在 **CAS 失败之后的处理**：CAS 失败说明别人刚改过状态，此时基于旧状态得出的
判定结论已经作废，必须重读并重新判断。

一个容易写错的地方：CAS 失败后直接报冲突。如果并发的是"另一个请求已经把它改成
同一个目标状态"，那么这次请求其实**已经成功**了，报冲突是错的——客户端会以为
取消没生效而反复重试。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.import_job import ImportJobStatus, PersistedImportJobRecord
from lyra.application.import_job_service import cancel_import_job, fail_import_job

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
JOB_ID = "44444444-4444-4444-4444-444444444444"


def _job(status: str = "uploaded") -> PersistedImportJobRecord:
    return PersistedImportJobRecord(
        id=JOB_ID, file_hash="a" * 64, original_file_name="ledger.csv", file_type="csv",
        file_size_bytes=2048, parser_version="2", status=status,  # type: ignore[arg-type]
        target_account_id=None, preview_token_hash=None, preview_ledger_version=None,
        total_rows=None, inserted_rows=None, skipped_rows=None, error_code=None,
        error_summary=None, committed_ledger_version=None,
        created_at="2026-10-12T08:00:00.000Z", updated_at="2026-10-12T08:00:00.000Z",
    )


class FakeStore:
    """内存仓储，可脚本化 CAS 结果以复现并发。"""

    def __init__(self, job: PersistedImportJobRecord | None, *, cas_fails: int = 0) -> None:
        self.job = job
        self.cas_fails = cas_fails
        self.cas_calls: list[tuple[str, str, str]] = []

    def load_import_job(self, job_id: str) -> PersistedImportJobRecord | None:
        return self.job

    def transition_status(
        self,
        job_id: str,
        expected_status: ImportJobStatus,
        next_status: ImportJobStatus,
        now: datetime,
    ) -> bool:
        self.cas_calls.append((job_id, expected_status, next_status))
        if self.cas_fails > 0:
            self.cas_fails -= 1
            # 模拟并发：别人把状态改成了目标状态。
            assert self.job is not None
            self.job = self.job.__class__(**{**self.job.__dict__, "status": next_status})
            return False
        assert self.job is not None
        if self.job.status != expected_status:
            return False
        self.job = self.job.__class__(**{**self.job.__dict__, "status": next_status})
        return True


# --------------------------------------------------------------------------
# 取消
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["uploaded", "preview_ready"])
def test_cancel_from_cancellable_states(status: str) -> None:
    """上传完成与预览就绪都可取消。"""
    store = FakeStore(_job(status))

    result = cancel_import_job(store, JOB_ID, NOW)

    assert result.status == "cancelled"
    assert store.cas_calls == [(JOB_ID, status, "cancelled")]


def test_cancel_is_idempotent_when_already_cancelled() -> None:
    """已取消时不再写库，直接返回当前状态。"""
    store = FakeStore(_job("cancelled"))

    result = cancel_import_job(store, JOB_ID, NOW)

    assert result.status == "cancelled"
    assert store.cas_calls == [], "已取消却仍然写库"


@pytest.mark.parametrize("status", ["committing", "committed", "failed", "rejected"])
def test_cancel_is_rejected_in_non_cancellable_states(status: str) -> None:
    """提交中不可取消；终态不可再变。"""
    store = FakeStore(_job(status))

    with pytest.raises(ApplicationError) as raised:
        cancel_import_job(store, JOB_ID, NOW)

    assert raised.value.code == "IMPORT_STATE_CONFLICT"
    assert raised.value.retryable is False


def test_cancel_unknown_job_reports_conflict_not_not_found() -> None:
    """不存在的作业报 409 而不是 404——不给未授权方探测作业是否存在。"""
    store = FakeStore(None)

    with pytest.raises(ApplicationError) as raised:
        cancel_import_job(store, JOB_ID, NOW)

    assert raised.value.code == "IMPORT_STATE_CONFLICT"
    assert raised.value.status_code == 409


# --------------------------------------------------------------------------
# CAS 失败后的重新判定
# --------------------------------------------------------------------------


def test_cas_failure_where_concurrent_request_reached_the_same_state_succeeds() -> None:
    """**并发请求已达成同一目标状态时，本次请求应视为成功而不是冲突。**

    直接报冲突会让客户端以为取消没生效而反复重试。
    """
    store = FakeStore(_job("uploaded"), cas_fails=1)

    result = cancel_import_job(store, JOB_ID, NOW)

    assert result.status == "cancelled"
    # 第一次 CAS 失败，随后基于新状态重新判定并直接返回。
    assert store.cas_calls == [(JOB_ID, "uploaded", "cancelled")]


def test_cas_failure_with_incompatible_new_state_is_not_retryable() -> None:
    """并发把状态改成了 `committing`（不可取消）时，报**不可重试**冲突。

    这里必须是非可重试：作业已经在提交中，反复重试取消永远不会成功；客户端应当
    刷新后看到 committing，而不是继续重试。
    """

    class RacingStore(FakeStore):
        """CAS 失败时把作业改成 committing（不可取消）。"""

        def transition_status(self, job_id, expected_status, next_status, now) -> bool:
            self.cas_calls.append((job_id, expected_status, next_status))
            assert self.job is not None
            self.job = self.job.__class__(**{**self.job.__dict__, "status": "committing"})
            return False

    store = RacingStore(_job("uploaded"))

    with pytest.raises(ApplicationError) as raised:
        cancel_import_job(store, JOB_ID, NOW)

    assert raised.value.code == "IMPORT_STATE_CONFLICT"
    assert raised.value.retryable is False
    assert raised.value.details == {
        "current_status": "committing",
        "requested_status": "cancelled",
    }


# --------------------------------------------------------------------------
# 失败标记
# --------------------------------------------------------------------------


def test_fail_from_parsing_and_committing() -> None:
    """解析中与提交中都允许标记失败。"""
    for status in ("parsing", "committing"):
        store = FakeStore(_job(status))
        assert fail_import_job(
            store, JOB_ID, error_code="IMPORT_VALIDATION_FAILED", now=NOW
        ).status == "failed"


def test_fail_rejects_unstable_error_code_before_any_read() -> None:
    """非稳定错误码必须在读库之前就被拒绝，不能写进可公开的作业记录。"""
    store = FakeStore(_job("parsing"))

    with pytest.raises(ValueError, match="稳定集合"):
        fail_import_job(
            store, JOB_ID, error_code="PSQLException: postgres://user:pw@host", now=NOW
        )
    assert store.cas_calls == []


def test_fail_rejects_terminal_states() -> None:
    """已提交的作业不能被改成失败。"""
    store = FakeStore(_job("committed"))

    with pytest.raises(ApplicationError):
        fail_import_job(store, JOB_ID, error_code="INTERNAL_ERROR", now=NOW)


def test_public_view_is_returned_not_the_raw_record() -> None:
    """编排返回的必须是公开视图，不含提交秘密与错误详情。"""
    store = FakeStore(_job("preview_ready"))

    result = cancel_import_job(store, JOB_ID, NOW)

    assert not hasattr(result, "preview_token_hash")
    assert not hasattr(result, "file_hash")
    assert result.id == JOB_ID
