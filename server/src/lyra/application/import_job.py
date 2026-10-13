"""导入作业的状态机与公开视图。

镜像 TypeScript 版 ``import-job.ts`` 与 ``import-job-view.ts``。

导入是**多步流程**（上传 → 解析 → 预览就绪 → 提交中 → 已提交），每一步都可能
被取消、拒绝或失败。用一张显式的转换图来管，而不是散落的 if：

- **终态不存在出边**——已提交/已拒绝/已失败/已取消的作业不能再变化；
- **同状态请求视为幂等重放**并返回原对象，调用方据此判断无需写库；
- 非法转换必须报冲突，而不是静默接受——否则"先预览再提交"的顺序会被绕过。

公开视图另有一条安全语义：**非稳定错误码被显式拒绝**，不能当自由文本透传给 App。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

from .app_error import ApplicationError

ImportJobStatus = Literal[
    "uploaded",
    "parsing",
    "preview_ready",
    "committing",
    "committed",
    "rejected",
    "failed",
    "cancelled",
]

# 合法转换图；终态不存在出边。
IMPORT_JOB_TRANSITIONS: dict[ImportJobStatus, tuple[ImportJobStatus, ...]] = {
    "uploaded": ("parsing", "cancelled"),
    "parsing": ("preview_ready", "rejected", "failed"),
    "preview_ready": ("committing", "cancelled"),
    "committing": ("committed", "failed"),
    "committed": (),
    "rejected": (),
    "failed": (),
    "cancelled": (),
}

# 导入作业允许持久化并公开的稳定错误码。
IMPORT_JOB_ERROR_CODES: tuple[str, ...] = (
    "IMPORT_UNSUPPORTED_FORMAT",
    "IMPORT_UNKNOWN_HEADERS",
    "IMPORT_AMBIGUOUS_SHEET",
    "IMPORT_VALIDATION_FAILED",
    "IMPORT_PREVIEW_STALE",
    "IMPORT_STATE_CONFLICT",
    "LEDGER_VERSION_CONFLICT",
    "LEDGER_REBUILD_FAILED",
    "INTERNAL_ERROR",
)


@dataclass(frozen=True)
class ImportJobState:
    """作业状态与其最近更新时间。"""

    import_id: str
    status: ImportJobStatus
    updated_at: str


@dataclass(frozen=True)
class PersistedImportJobRecord:
    """与 DuckDB ``import_files`` 对齐的完整导入作业记录。"""

    id: str
    file_hash: str
    original_file_name: str
    file_type: Literal["csv", "xlsx"]
    file_size_bytes: int
    parser_version: str
    status: ImportJobStatus
    target_account_id: str | None
    preview_token_hash: str | None
    preview_ledger_version: int | None
    total_rows: int | None
    inserted_rows: int | None
    skipped_rows: int | None
    error_code: str | None
    error_summary: str | None
    committed_ledger_version: int | None
    created_at: str
    updated_at: str
    # 预览指纹与令牌有效期：提交时用于授权校验。默认值让既有构造点无需改动。
    preview_fingerprint: str | None = None
    preview_expires_at: str | None = None


@dataclass(frozen=True)
class PublicImportJob:
    """App 恢复导入流程所需的最小公开作业状态。"""

    id: str
    file_name: str
    file_type: Literal["csv", "xlsx"]
    file_size_bytes: int
    status: ImportJobStatus
    target_account_id: str | None
    base_ledger_version: int | None
    total_rows: int | None
    inserted_rows: int | None
    skipped_rows: int | None
    error_code: str | None
    committed_ledger_version: int | None
    created_at: str
    updated_at: str


def transition_import_job(
    current: ImportJobState,
    next_status: ImportJobStatus,
    now: datetime,
) -> ImportJobState:
    """验证并生成一次导入作业状态更新。

    **同状态请求返回原对象本身**（而不是等价副本）：调用方据此判断是否需要写库，
    返回副本会让幂等重放也产生一次无收益写入。

    持久化层仍须用 ``WHERE status = current.status`` 做比较并交换更新——本函数只
    判定转换合法性，不负责并发。

    @param current: 数据库读取的当前作业状态。
    @param next_status: 请求进入的目标状态。
    @param now: 服务端可信时钟。
    @returns 更新后的状态，或同状态时的原对象。
    @raises ValueError: 目标状态不在转换图中，或时间无时区。
    @raises ApplicationError: 转换非法（409）。
    """
    if next_status not in IMPORT_JOB_TRANSITIONS:
        raise ValueError(f"未知的导入作业状态: {next_status}")
    if current.status == next_status:
        return current
    if next_status not in IMPORT_JOB_TRANSITIONS[current.status]:
        raise ApplicationError(
            code="IMPORT_STATE_CONFLICT",
            message="导入作业状态已变化，请刷新后重试",
            status_code=409,
            retryable=False,
            details={
                "current_status": current.status,
                "requested_status": next_status,
            },
        )
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("导入作业更新时间必须带时区")
    return replace(current, status=next_status, updated_at=_iso(now))


def create_public_import_job(job: PersistedImportJobRecord) -> PublicImportJob:
    """把完整导入记录投影为 App 可安全轮询和恢复的状态。

    刻意省略：文件哈希、解析器版本、预览令牌摘要、底层错误详情——它们只用于服务端
    提交校验与日志。

    @param job: 仓储读取的完整导入作业记录。
    @returns 不含提交秘密与内部错误详情的公开状态。
    @raises ValueError: 作业带有非稳定错误码。
    """
    if job.error_code is not None and not is_import_job_error_code(job.error_code):
        raise ValueError(f"导入作业包含非稳定错误码: {job.error_code}")
    return PublicImportJob(
        id=job.id,
        file_name=job.original_file_name,
        file_type=job.file_type,
        file_size_bytes=job.file_size_bytes,
        status=job.status,
        target_account_id=job.target_account_id,
        base_ledger_version=job.preview_ledger_version,
        total_rows=job.total_rows,
        inserted_rows=job.inserted_rows,
        skipped_rows=job.skipped_rows,
        error_code=job.error_code,
        committed_ledger_version=job.committed_ledger_version,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def is_import_job_error_code(value: str) -> bool:
    """判断仓储值是否属于导入作业可公开错误码闭集。"""
    return value in IMPORT_JOB_ERROR_CODES


def _iso(value: datetime) -> str:
    """渲染为与 JavaScript ``toISOString()`` 同形的文本。"""
    return (
        value.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
