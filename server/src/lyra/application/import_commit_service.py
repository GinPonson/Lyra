"""导入提交编排。

把前几轮各自的部件按**正确顺序**串起来：

```
1. 读取作业
2. 校验预览授权（作业 ID / 指纹 / 状态 / 有效期 / 令牌）
3. CAS：preview_ready → committing      ← 一次性消费发生在这里
4. 账本提交（内部：再校验版本 + 单事务写交易并替换 lots/realized + 作业终态）
5. 失败则 CAS：committing → failed
```

**第 3 步必须在第 4 步之前**。若先写账本再改状态，两个并发提交会都通过授权校验、
都去写账本，交易被导入两次。CAS 是唯一能挡住这件事的地方——授权校验只能证明
"这个令牌曾经有效"，不能证明"它还没被用过"。

**第 3 步与第 4 步不在同一事务**：把状态 CAS 也搬进账本事务，需要本模块反过来接管
``import_files`` 终态写入，并按 DuckDB 的外键顺序把它排在 ``transactions`` 插入之前
（父行一旦被引用就不能再更新）——那是结构性改动，涉及服务与仓储的接口契约。

因此当前保留既定分层，但**补上了恢复路径**：进入提交前，处于 ``committing`` 且
**账本确认未写入**的作业会被判回 ``preview_ready`` 后正常重试；否则那个作业既不能提交
（状态已不是 ``preview_ready``）也不能取消，用户被迫重新上传。账本已写入但状态仍停在
``committing`` 的极端情形不做自动回退：那需要按导入作业 UUID 找到账本版本再补记终态，
留待运维核对。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .app_error import ApplicationError
from .import_commit import ImportCommitPlan
from .import_job import PersistedImportJobRecord
from .import_job_service import ImportJobStore
from .import_preview_auth import (
    ImportPreviewAuthorizationRecord,
    assert_import_preview_authorized,
)


class ImportCommitResult:
    """提交结果。"""

    __slots__ = ("changed", "inserted_rows", "ledger_version", "skipped_rows")

    def __init__(
        self, *, changed: bool, ledger_version: int, inserted_rows: int, skipped_rows: int
    ) -> None:
        self.changed = changed
        self.ledger_version = ledger_version
        self.inserted_rows = inserted_rows
        self.skipped_rows = skipped_rows

    def __repr__(self) -> str:
        return (
            f"ImportCommitResult(changed={self.changed}, "
            f"ledger_version={self.ledger_version}, inserted_rows={self.inserted_rows}, "
            f"skipped_rows={self.skipped_rows})"
        )


def commit_import_job(
    *,
    store: ImportJobStore,
    import_id: str,
    preview_record: ImportPreviewAuthorizationRecord,
    preview_fingerprint: str,
    token: str,
    plan: ImportCommitPlan,
    now: datetime,
    commit_ledger: Callable[[int], ImportCommitResult],
) -> ImportCommitResult:
    """校验授权、原子消费令牌并提交账本。

    @param store: 作业仓储（CAS 语义）。
    @param import_id: 作业 ID。
    @param preview_record: 预览阶段的授权记录。
    @param preview_fingerprint: 本次提交携带的预览指纹。
    @param token: App 提交的明文令牌。
    @param plan: 事务外已算好的提交计划。
    @param now: 服务端可信时间。
    @param commit_ledger: 执行账本写入；接收期望账本版本，返回提交结果。
    @returns 提交结果。
    @raises ApplicationError: 授权失效（409）、状态冲突（409）或账本版本冲突（409）。
    @raises ValueError: 作业不存在。
    """
    job = store.load_import_job(import_id)
    if job is None:
        raise ValueError(f"导入作业不存在: {import_id}")

    # **崩溃恢复要先于授权校验。** CAS 与账本事务之间的窗口里崩溃会留下 committing
    # 且账本未写入的作业，而授权校验看到 committing 会直接判 409——那个作业就再也
    # 无法提交，用户只能重新上传。只有仓储确认账本未写入时才会真正回退。
    if job.status == "committing" and store.recover_stale_committing(import_id, now):
        recovered = store.load_import_job(import_id)
        if recovered is not None:
            job = recovered

    assert_import_preview_authorized(
        record=preview_record,
        import_id=import_id,
        preview_fingerprint=preview_fingerprint,
        token=token,
        job_status=job.status,
        now=now,
    )

    # 一次性消费：只有仍处于 preview_ready 的作业能进入 committing。
    if not store.transition_status(import_id, "preview_ready", "committing", now):
        raise ApplicationError(
            code="IMPORT_STATE_CONFLICT",
            message="导入作业状态已变化，请刷新后重试",
            status_code=409,
            retryable=False,
            details={"current_status": job.status, "requested_status": "committing"},
        )

    expected_version = _preview_base_version(job)
    try:
        result = commit_ledger(expected_version)
    except Exception:
        # 令牌已被消费；作业进入 failed，用户必须重新预览才能再试。
        store.transition_status(import_id, "committing", "failed", now)
        raise

    # **账本版本与状态一次写入**：分开写会让重放期间读到 committed 但版本仍为 NULL，
    # 于是回放返回一个与首次提交不同的 ledgerVersion。
    store.transition_status(
        import_id, "committing", "committed", now,
        committed_ledger_version=result.ledger_version,
    )
    return result


def _preview_base_version(job: PersistedImportJobRecord) -> int:
    """取出预览时记录的账本基准版本。

    ``NULL`` **表示预览时是空账本，基准为 0**，而不是"缺失"。

    数据库里之所以用 NULL 而不是 0：``import_files.preview_ledger_version`` 引用
    ``ledger_versions(version)``，而空账本根本没有 version 0 这一行。若这里把 NULL
    当成错误，**空账本上的首次导入将永远无法提交**。
    """
    return job.preview_ledger_version if job.preview_ledger_version is not None else 0
