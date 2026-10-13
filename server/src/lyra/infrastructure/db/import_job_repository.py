"""导入作业的 DuckDB 仓储。

镜像 TypeScript 版 ``import-job-repository.ts`` 的核心：读取作业、创建作业，以及
**比较并交换（CAS）的状态转换**。

为什么状态更新必须是 CAS：
:func:`transition_import_job` 只在内存里判定转换合法性，而两个并发请求可能读到
同一个旧状态、各自判定合法、然后都去写库。用
``UPDATE ... WHERE id = ? AND status = ?`` 让数据库来做唯一裁决——只有一方能改成功，
另一方拿到 0 行受影响，必须重新读取后再决定。

时间戳一律以 ISO UTC 文本进出：领域层按文本比较时间，返回 ``datetime`` 会让比较
静默按本地时区解释。
"""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb

from ...application.app_error import ApplicationError
from ...application.import_job import (
    IMPORT_JOB_TRANSITIONS,
    ImportJobStatus,
    PersistedImportJobRecord,
)


class DuckDbImportJobRepository:
    """导入作业仓储。

    @param connection: 已迁移到当前 schema 的连接；写操作由单写队列串行化。
    """

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection

    def create_import_job(self, record: PersistedImportJobRecord) -> None:
        """插入一条新作业记录。"""
        self._connection.execute(
            """
            INSERT INTO import_files (
                id, file_hash, original_file_name, file_type, file_size_bytes,
                parser_version, status, target_account_id, preview_token_hash,
                preview_ledger_version, total_rows, inserted_rows, skipped_rows,
                error_code, error_summary, committed_ledger_version, created_at, updated_at
            ) VALUES (
                CAST(? AS UUID), ?, ?, ?, ?, ?, ?, CAST(? AS UUID), ?, ?, ?, ?, ?, ?, ?, ?,
                CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ)
            )
            """,
            [
                record.id, record.file_hash, record.original_file_name, record.file_type,
                record.file_size_bytes, record.parser_version, record.status,
                record.target_account_id, record.preview_token_hash,
                record.preview_ledger_version, record.total_rows, record.inserted_rows,
                record.skipped_rows, record.error_code, record.error_summary,
                record.committed_ledger_version, record.created_at, record.updated_at,
            ],
        )

    def load_import_job(self, job_id: str) -> PersistedImportJobRecord | None:
        """按 ID 读取作业；不存在返回 ``None``。"""
        row = self._connection.execute(
            """
            SELECT id, file_hash, original_file_name, file_type, file_size_bytes,
                   parser_version, status, target_account_id, preview_token_hash,
                   preview_ledger_version, total_rows, inserted_rows, skipped_rows,
                   error_code, error_summary, committed_ledger_version, created_at, updated_at,
                   preview_fingerprint, preview_expires_at
            FROM import_files WHERE id = CAST(? AS UUID)
            """,
            [job_id],
        ).fetchone()
        if row is None:
            return None
        return PersistedImportJobRecord(
            id=str(row[0]),
            file_hash=row[1],
            original_file_name=row[2],
            file_type=row[3],
            file_size_bytes=int(row[4]),
            parser_version=row[5],
            status=row[6],
            target_account_id=None if row[7] is None else str(row[7]),
            preview_token_hash=row[8],
            preview_ledger_version=None if row[9] is None else int(row[9]),
            total_rows=None if row[10] is None else int(row[10]),
            inserted_rows=None if row[11] is None else int(row[11]),
            skipped_rows=None if row[12] is None else int(row[12]),
            error_code=row[13],
            error_summary=row[14],
            committed_ledger_version=None if row[15] is None else int(row[15]),
            created_at=_iso(row[16]),
            updated_at=_iso(row[17]),
            preview_fingerprint=row[18],
            preview_expires_at=None if row[19] is None else _iso(row[19]),
        )

    def transition_status(
        self,
        job_id: str,
        expected_status: ImportJobStatus,
        next_status: ImportJobStatus,
        now: datetime,
        committed_ledger_version: int | None = None,
    ) -> bool:
        """以比较并交换方式推进作业状态。

        @param job_id: 作业 ID。
        @param expected_status: 调用方读到的当前状态。
        @param next_status: 目标状态（已由 :func:`transition_import_job` 判定合法）。
        @param now: 更新时间。
        @param committed_ledger_version: 提交产生的账本版本；仅在
            ``→ committed`` 时给出。
        @returns 是否成功；``False`` 表示状态已被并发请求改动，调用方须重新读取。

        **账本版本必须与状态在同一次更新里写入。** 分两次写会出现"状态已 committed
        但版本还没落"的窗口；期间的重放请求会退回基准版本，**返回一个与首次提交不同
        的 ledgerVersion**——客户端据此判断"是否产生了新版本"就会出错。
        @raises ApplicationError: 转换不在合法图内（409）。

        这里**重复校验转换图**（领域层已校验一次）是刻意的防御：仓储只做 CAS 的话，
        调用方一旦忘记先调 :func:`transition_import_job`，就能把「已提交」写成
        「提交中」——而账本已经写过了。
        """
        if next_status not in IMPORT_JOB_TRANSITIONS.get(expected_status, ()):
            raise ApplicationError(
                code="IMPORT_STATE_CONFLICT",
                message="导入作业状态已变化，请刷新后重试",
                status_code=409,
                retryable=False,
                details={
                    "current_status": expected_status,
                    "requested_status": next_status,
                },
            )
        row = self._connection.execute(
            """
            UPDATE import_files
            SET status = ?, updated_at = CAST(? AS TIMESTAMPTZ),
                committed_ledger_version = coalesce(?, committed_ledger_version)
            WHERE id = CAST(? AS UUID) AND status = ?
            RETURNING id
            """,
            [next_status, _iso(now), committed_ledger_version, job_id, expected_status],
        ).fetchone()
        return row is not None

    def record_preview(
        self,
        job_id: str,
        *,
        target_account_id: str,
        preview_token_hash: str,
        preview_fingerprint: str,
        preview_expires_at: datetime,
        preview_ledger_version: int,
        total_rows: int,
        inserted_rows: int,
        skipped_rows: int,
        now: datetime,
    ) -> bool:
        """在解析完成后写入预览结果，并把状态推进到 ``preview_ready``。

        与 :meth:`transition_status` 同理使用 CAS：只有仍处于 ``parsing`` 的作业
        才能被写入预览结果。

        @returns 是否成功。
        """
        row = self._connection.execute(
            """
            UPDATE import_files
            SET status = 'preview_ready',
                target_account_id = CAST(? AS UUID),
                preview_token_hash = ?,
                preview_fingerprint = ?,
                preview_expires_at = CAST(? AS TIMESTAMPTZ),
                preview_ledger_version = ?,
                total_rows = ?, inserted_rows = ?, skipped_rows = ?,
                updated_at = CAST(? AS TIMESTAMPTZ)
            WHERE id = CAST(? AS UUID) AND status = 'parsing'
            RETURNING id
            """,
            [
                target_account_id, preview_token_hash, preview_fingerprint,
                _iso(preview_expires_at), preview_ledger_version,
                total_rows, inserted_rows, skipped_rows, _iso(now), job_id,
            ],
        ).fetchone()
        return row is not None


def _iso(value: object) -> str:
    """把 DuckDB 返回的时间值渲染为与 JavaScript ``toISOString()`` 同形的文本。"""
    if isinstance(value, datetime):
        return (
            value.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return str(value)
