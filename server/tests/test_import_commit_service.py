"""导入提交编排测试。

这个文件的核心不是"提交成功"，而是**顺序**：

1. **令牌在写账本之前就被消费。** 若顺序颠倒，两个并发提交会都通过授权校验、
   都去写账本，交易被导入两次。授权校验只能证明"令牌曾经有效"，不能证明"它还没
   被用过"——CAS 是唯一能挡住这件事的地方。
2. **账本写入失败时作业进入 failed**，而不是留在 committing 或退回 preview_ready。
   退回 preview_ready 会让同一个令牌可以再试，而账本可能已经写了一半。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.import_commit import ImportCommitPlan
from lyra.application.import_commit_service import ImportCommitResult, commit_import_job
from lyra.application.import_job import ImportJobStatus, PersistedImportJobRecord
from lyra.application.import_preview_auth import (
    issue_import_preview_authorization,
)
from lyra.domain.ledger.types import ParsedTransaction

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
IMPORT_ID = "44444444-4444-4444-4444-444444444444"
FINGERPRINT = "a" * 64


def _job(status: str = "preview_ready", version: int | None = 3) -> PersistedImportJobRecord:
    return PersistedImportJobRecord(
        id=IMPORT_ID, file_hash="b" * 64, original_file_name="ledger.csv", file_type="csv",
        file_size_bytes=2048, parser_version="2", status=status,  # type: ignore[arg-type]
        target_account_id=None, preview_token_hash="c" * 64,
        preview_ledger_version=version, total_rows=1, inserted_rows=1, skipped_rows=0,
        error_code=None, error_summary=None, committed_ledger_version=None,
        created_at="2026-10-12T08:00:00.000Z", updated_at="2026-10-12T08:30:00.000Z",
    )


class FakeStore:
    """内存作业仓储，记录 CAS 调用顺序。"""

    def __init__(self, job: PersistedImportJobRecord | None) -> None:
        self.job = job
        self.cas_calls: list[tuple[str, str]] = []

    def load_import_job(self, job_id: str) -> PersistedImportJobRecord | None:
        return self.job

    def transition_status(
        self,
        job_id: str,
        expected_status: ImportJobStatus,
        next_status: ImportJobStatus,
        now: datetime,
        committed_ledger_version: int | None = None,
    ) -> bool:
        self.cas_calls.append((expected_status, next_status))
        if self.job is None or self.job.status != expected_status:
            return False
        self.job = replace(self.job, status=next_status)
        return True


def _issued_token():
    """签发令牌并返回 (记录, 明文)。"""
    issued = issue_import_preview_authorization(IMPORT_ID, FINGERPRINT, NOW, 900_000)
    return issued.record, issued.token


def _plan() -> ImportCommitPlan:
    """构造一个变更计划（内容不重要，编排只关心顺序）。"""
    transaction = ParsedTransaction(
        account="TST001", source_type="csv", source_reference="ledger.csv",
        record_date="2026-01-05", transaction_date="2026-01-05", action="Buy",
        raw_symbol="INTC", description="INTEL", asset_type="stock", underlying="INTC",
        expiry=None, strike=None, option_type=None,
        quantity=Decimal(10), price=Decimal(40), fees=Decimal(0),
        amount=Decimal(-400), source_hash="d" * 64, sequence=1, occurrence=1,
        business_hash="e" * 64, transaction_hash="f" * 64,
    )
    return ImportCommitPlan(
        account="TST001", changed=True, inserted_rows=1, skipped_rows=0,
        inserted_transactions=(transaction,), account_transactions=(transaction,), fifo=None,
    )


def _ok(version: int) -> ImportCommitResult:
    return ImportCommitResult(
        changed=True, ledger_version=version + 1, inserted_rows=1, skipped_rows=0
    )


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------


def test_successful_commit_advances_through_committing() -> None:
    """成功提交必须经过 committing 再到 committed。"""
    store = FakeStore(_job())
    record, token = _issued_token()

    result = commit_import_job(
        store=store, import_id=IMPORT_ID, preview_record=record,
        preview_fingerprint=FINGERPRINT, token=token, plan=_plan(), now=NOW,
        commit_ledger=_ok,
    )

    assert result.ledger_version == 4
    assert store.cas_calls == [
        ("preview_ready", "committing"),
        ("committing", "committed"),
    ]
    assert store.job is not None and store.job.status == "committed"


def test_ledger_receives_the_preview_baseline_version() -> None:
    """账本提交必须拿到**预览时记录的**基准版本，而不是重新读一个。"""
    store = FakeStore(_job(version=7))
    record, token = _issued_token()
    seen: list[int] = []

    commit_import_job(
        store=store, import_id=IMPORT_ID, preview_record=record,
        preview_fingerprint=FINGERPRINT, token=token, plan=_plan(), now=NOW,
        commit_ledger=lambda version: (seen.append(version), _ok(version))[1],
    )

    assert seen == [7]


# --------------------------------------------------------------------------
# 顺序与消费语义
# --------------------------------------------------------------------------


def test_token_is_consumed_before_the_ledger_is_written() -> None:
    """**令牌必须在写账本之前被消费。**

    若顺序颠倒，两个并发提交会都通过授权校验、都去写账本，交易被导入两次。
    """
    store = FakeStore(_job())
    record, token = _issued_token()

    def commit_ledger(version: int) -> ImportCommitResult:
        # 账本开始时，作业必须已经处于 committing。
        assert store.job is not None
        assert store.job.status == "committing", "写账本时作业仍未离开 preview_ready"
        return _ok(version)

    commit_import_job(
        store=store, import_id=IMPORT_ID, preview_record=record,
        preview_fingerprint=FINGERPRINT, token=token, plan=_plan(), now=NOW,
        commit_ledger=commit_ledger,
    )


def test_second_commit_with_the_same_token_is_rejected() -> None:
    """同一个令牌第二次提交必须被 CAS 挡住。"""
    store = FakeStore(_job())
    record, token = _issued_token()
    commit_import_job(
        store=store, import_id=IMPORT_ID, preview_record=record,
        preview_fingerprint=FINGERPRINT, token=token, plan=_plan(), now=NOW,
        commit_ledger=_ok,
    )

    with pytest.raises(ApplicationError) as raised:
        commit_import_job(
            store=store, import_id=IMPORT_ID, preview_record=record,
            preview_fingerprint=FINGERPRINT, token=token, plan=_plan(), now=NOW,
            commit_ledger=_ok,
        )

    # 第二次在授权校验阶段就被拒（状态已是 committed）。
    assert raised.value.code == "IMPORT_PREVIEW_STALE"
    assert raised.value.details == {"changed_fields": ["job_status"]}


def test_concurrent_commit_that_loses_the_cas_is_rejected() -> None:
    """CAS 输掉的一方报状态冲突，而不是照样写账本。"""

    class RacingStore(FakeStore):
        """第二次 load 返回 committing，模拟并发已抢到。"""

        def __init__(self, job) -> None:
            super().__init__(job)
            self.loads = 0

        def load_import_job(self, job_id: str):
            self.loads += 1
            if self.loads == 1:
                return self.job
            # 另一个请求已经把状态推到 committing。
            return replace(self.job, status="committing") if self.job is not None else None

    store = RacingStore(_job())
    record, token = _issued_token()
    wrote: list[int] = []

    # 第一次 load 看到 preview_ready 通过授权，但 CAS 时状态已不是 preview_ready。

    def transition(job_id, expected, next_status, now):
        store.cas_calls.append((expected, next_status))
        return False

    store.transition_status = transition  # type: ignore[method-assign]

    with pytest.raises(ApplicationError) as raised:
        commit_import_job(
            store=store, import_id=IMPORT_ID, preview_record=record,
            preview_fingerprint=FINGERPRINT, token=token, plan=_plan(), now=NOW,
            commit_ledger=lambda version: (wrote.append(version), _ok(version))[1],
        )

    assert raised.value.code == "IMPORT_STATE_CONFLICT"
    assert wrote == [], "CAS 输掉却仍然写了账本"


# --------------------------------------------------------------------------
# 失败路径
# --------------------------------------------------------------------------


def test_ledger_failure_marks_the_job_failed() -> None:
    """账本写入失败必须把作业标记为 failed，而不是留在 committing。"""
    store = FakeStore(_job())
    record, token = _issued_token()

    def commit_ledger(version: int) -> ImportCommitResult:
        raise ApplicationError(
            code="LEDGER_VERSION_CONFLICT",
            message="账本版本已变化",
            status_code=409,
            retryable=False,
        )

    with pytest.raises(ApplicationError) as raised:
        commit_import_job(
            store=store, import_id=IMPORT_ID, preview_record=record,
            preview_fingerprint=FINGERPRINT, token=token, plan=_plan(), now=NOW,
            commit_ledger=commit_ledger,
        )

    assert raised.value.code == "LEDGER_VERSION_CONFLICT"
    assert store.cas_calls == [
        ("preview_ready", "committing"),
        ("committing", "failed"),
    ]
    assert store.job is not None and store.job.status == "failed"


def test_failed_commit_cannot_be_retried_with_the_same_token() -> None:
    """失败后同一令牌不能重试——必须重新预览。

    退回 preview_ready 会让同一个令牌再试一次，而账本可能已经写了一半。
    """
    store = FakeStore(_job())
    record, token = _issued_token()

    def failing(version: int) -> ImportCommitResult:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        commit_import_job(
            store=store, import_id=IMPORT_ID, preview_record=record,
            preview_fingerprint=FINGERPRINT, token=token, plan=_plan(), now=NOW,
            commit_ledger=failing,
        )

    with pytest.raises(ApplicationError) as raised:
        commit_import_job(
            store=store, import_id=IMPORT_ID, preview_record=record,
            preview_fingerprint=FINGERPRINT, token=token, plan=_plan(), now=NOW,
            commit_ledger=_ok,
        )
    assert raised.value.details == {"changed_fields": ["job_status"]}


def test_authorization_failure_never_touches_the_ledger() -> None:
    """授权失败时不得写账本，也不得改动作业状态。"""
    store = FakeStore(_job())
    record, _ = _issued_token()
    wrote: list[int] = []

    with pytest.raises(ApplicationError) as raised:
        commit_import_job(
            store=store, import_id=IMPORT_ID, preview_record=record,
            preview_fingerprint=FINGERPRINT, token="wrong-token", plan=_plan(), now=NOW,
            commit_ledger=lambda version: wrote.append(version),
        )

    assert raised.value.code == "IMPORT_PREVIEW_STALE"
    assert wrote == []
    assert store.cas_calls == []
    assert store.job is not None and store.job.status == "preview_ready"


def test_unknown_job_is_rejected() -> None:
    """不存在的作业必须拒绝。"""
    store = FakeStore(None)
    record, token = _issued_token()

    with pytest.raises(ValueError, match="不存在"):
        commit_import_job(
            store=store, import_id=IMPORT_ID, preview_record=record,
            preview_fingerprint=FINGERPRINT, token=token, plan=_plan(), now=NOW,
            commit_ledger=_ok,
        )


def test_null_baseline_means_empty_ledger_not_missing() -> None:
    """``preview_ledger_version`` 为 NULL 表示**预览时是空账本**，基准为 0。

    数据库里用 NULL 而不是 0，是因为该列引用 ``ledger_versions(version)``，而空账本
    没有 version 0 这一行。若把 NULL 当成"缺失"而拒绝，**空账本上的首次导入将永远
    无法提交**。
    """
    store = FakeStore(_job(version=None))
    record, token = _issued_token()
    seen: list[int] = []

    commit_import_job(
        store=store, import_id=IMPORT_ID, preview_record=record,
        preview_fingerprint=FINGERPRINT, token=token, plan=_plan(), now=NOW,
        commit_ledger=lambda version: (seen.append(version), _ok(version))[1],
    )

    assert seen == [0], "空账本基准版本应为 0"
