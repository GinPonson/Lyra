"""导入作业的生命周期编排。

负责领域状态机（:mod:`lyra.application.import_job`）与仓储 CAS 之间的衔接：

1. 读取作业 → 2. 判定转换是否合法 → 3. CAS 写入 → 4. CAS 失败则重读并重新判定

第 4 步是必须的：CAS 失败说明**别人刚改过**，此时旧状态上的判定结论已经作废，
不能再拿它去报错或重试，必须基于新状态重新判断。
"""

from __future__ import annotations

from datetime import datetime

from .app_error import ApplicationError
from .import_job import (
    ImportJobState,
    ImportJobStatus,
    PersistedImportJobRecord,
    PublicImportJob,
    create_public_import_job,
    is_import_job_error_code,
    transition_import_job,
)


class ImportJobStore:
    """编排所需的仓储能力。

    实现方必须让 :meth:`transition_status` 是**比较并交换**语义：只在当前状态
    等于 ``expected_status`` 时才会改写并返回 ``True``。
    """

    def load_import_job(self, job_id: str) -> PersistedImportJobRecord | None: ...

    def transition_status(
        self,
        job_id: str,
        expected_status: ImportJobStatus,
        next_status: ImportJobStatus,
        now: datetime,
    ) -> bool: ...


def cancel_import_job(store: ImportJobStore, job_id: str, now: datetime) -> PublicImportJob:
    """取消一个尚未开始提交的导入作业。

    已取消时是幂等的（返回当前状态）；正在提交中或已终态的作业会被状态机拒绝。

    @param store: 作业仓储。
    @param job_id: 作业 ID。
    @param now: 服务端可信时间。
    @returns 取消后的公开作业状态。
    @raises ApplicationError: 作业不存在，或状态不允许取消（409）。
    """
    return _advance(store, job_id, "cancelled", now)


def fail_import_job(
    store: ImportJobStore,
    job_id: str,
    *,
    error_code: str,
    now: datetime,
) -> PublicImportJob:
    """把作业标记为失败。

    @param store: 作业仓储。
    @param job_id: 作业 ID。
    @param error_code: **必须是稳定错误码**；自由文本会被拒绝。
    @param now: 服务端可信时间。
    @returns 失败后的公开作业状态。
    @raises ValueError: 错误码不在稳定集合内。
    @raises ApplicationError: 作业不存在，或状态不允许失败（409）。
    """
    if not is_import_job_error_code(error_code):
        # 宁可拒绝，也不把可能含内部细节的文本写进可公开的作业记录。
        raise ValueError(f"导入作业错误码不在稳定集合内: {error_code}")
    return _advance(store, job_id, "failed", now)


def _advance(
    store: ImportJobStore,
    job_id: str,
    next_status: ImportJobStatus,
    now: datetime,
) -> PublicImportJob:
    """读取 → 判定 → CAS 推进；CAS 失败后基于新状态重新判定一次。

    @param store: 作业仓储。
    @param job_id: 作业 ID。
    @param next_status: 目标状态。
    @param now: 服务端可信时间。
    @returns 推进后的公开作业状态。
    @raises ApplicationError: 作业不存在（409），或转换非法（409）。
    """
    job = _require_job(store, job_id)
    state = _state_of(job)
    # 第一次判定：合法则尝试 CAS；不合法会直接抛出冲突。
    candidate = transition_import_job(state, next_status, now)
    if candidate is state:
        # 已是目标状态，无需写库。
        return create_public_import_job(job)

    if store.transition_status(job_id, state.status, next_status, now):
        return create_public_import_job(job.__class__(**{**job.__dict__, "status": next_status}))

    # CAS 失败：别人刚改过，旧状态上的判定结论已作废，必须基于新状态重新判断。
    refreshed = _require_job(store, job_id)
    refreshed_state = _state_of(refreshed)
    # 新状态若已等于目标（并发的另一请求达成了同一结果），本次请求即视为成功。
    # 直接报冲突会让客户端以为取消没生效而反复重试。
    if transition_import_job(refreshed_state, next_status, now) is refreshed_state:
        return create_public_import_job(refreshed)

    # 新状态允许该转换，再试一次 CAS：两个并发取消应当都能成功。
    if store.transition_status(job_id, refreshed_state.status, next_status, now):
        return create_public_import_job(
            refreshed.__class__(**{**refreshed.__dict__, "status": next_status})
        )

    # 两次都失败：确实存在并发热点，交由调用方重试，而不是假装成功。
    raise ApplicationError(
        code="IMPORT_STATE_CONFLICT",
        message="导入作业状态已变化，请刷新后重试",
        status_code=409,
        retryable=True,
        details={
            "current_status": refreshed_state.status,
            "requested_status": next_status,
        },
    )


def _require_job(store: ImportJobStore, job_id: str) -> PersistedImportJobRecord:
    """读取作业；不存在时报冲突而不是 404 —— 不给未授权方探测作业是否存在。"""
    job = store.load_import_job(job_id)
    if job is None:
        raise ApplicationError(
            code="IMPORT_STATE_CONFLICT",
            message="导入作业状态已变化，请刷新后重试",
            status_code=409,
            retryable=False,
        )
    return job


def _state_of(job: PersistedImportJobRecord) -> ImportJobState:
    """从完整记录取出状态机所需的字段。"""
    return ImportJobState(import_id=job.id, status=job.status, updated_at=job.updated_at)
