"""导入作业的 HTTP 路由。

路径与 TypeScript 侧对齐：

| 路由 | 状态 |
| --- | --- |
| ``POST /v1/imports/preview`` | ✅ |
| ``GET /v1/imports/{importId}`` | ✅ |
| ``POST /v1/imports/{importId}/cancel`` | ✅ |
| ``POST /v1/imports/{importId}/commit`` | ✅ |

预览请求体是**原始字节**（``application/octet-stream``），文件名走
``x-lyra-file-name`` 头——与 TypeScript 侧一致，不使用 multipart。
预览响应与 Flutter ``ImportPreviewView.fromJson`` 对齐：根对象携带 ``importId`` 与
一次性 ``confirmToken``，统计与账户信息位于 ``preview`` 对象中。
"""

from __future__ import annotations

# 幂等记录的操作名。与 TS 侧的 ``const operation = "import.commit"`` 一致——
# 两侧必须用同一个字符串，否则同一台设备在切换前后会被视为两个不同的操作，
# 幂等键在切换那一刻失效。
IDEMPOTENCY_OPERATION = "import.commit"

# 明文确认令牌与幂等键的上限，与参考实现的 schema 一致。
# **上限是请求侧契约的一部分**：客户端的截断与重试逻辑按它设计。
_MAX_CONFIRM_TOKEN_LENGTH = 512
_MAX_IDEMPOTENCY_KEY_LENGTH = 200
_MIN_IDEMPOTENCY_KEY_LENGTH = 16

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from ..application.app_error import ApplicationError
from ..application.idempotent_mutation import (
    validate_idempotency_key,
    IdempotentMutationReplay,
    IdempotentMutationStart,
    hash_idempotency_key,
    plan_idempotent_mutation,
)
from ..application.import_account import ImportAccountRecord, resolve_import_account
from ..application.import_commit import ImportCommitPreview, build_import_commit_plan
from ..application.import_commit_service import commit_import_job
from ..application.import_job import PersistedImportJobRecord, create_public_import_job
from ..application.import_job_service import cancel_import_job
from ..application.import_prepare import parse_uploaded_file
from ..application.import_preview import CreateImportPreviewInput, create_import_preview
from ..application.import_preview_auth import (
    ImportPreviewAuthorizationRecord,
    issue_import_preview_authorization,
)
from ..application.public_projection import project_import_job, project_import_preview
from ..domain.analytics.types import AccountScope
from ..domain.ledger.batch import normalize_transaction_batch
from ..domain.ledger.errors import IngestError
from ..infrastructure.db.import_commit_repository import (
    PARSER_VERSION,
    DuckDbImportCommitRepository,
    IdempotencyCompletion,
)
from ..infrastructure.db.import_job_repository import DuckDbImportJobRepository
from ..infrastructure.db.ledger_repository import DuckDbLedgerRepository
from ..infrastructure.db.mutation_idempotency_repository import (
    load_mutation_idempotency_record,
    start_mutation_idempotency_record,
)
from ..infrastructure.imports.staged_upload import StagedUploadStore
from .dependencies import _UUID_PATTERN, require_device

# 预览令牌有效期：15 分钟。这是用户读预览并确认的时间窗。
PREVIEW_TTL_MILLISECONDS = 15 * 60 * 1000


def create_import_router() -> APIRouter:
    """构造导入作业路由。

    @returns 全部导入端点；均要求设备认证。
    """
    router = APIRouter(dependencies=[Depends(require_device)])

    def _store(request: Request) -> DuckDbImportJobRepository:
        """按应用持有的数据库构造作业仓储。"""
        return DuckDbImportJobRepository(request.app.state.database.connection)

    @router.post("/v1/imports/preview", status_code=201)
    async def create_preview(
        request: Request,
        x_lyra_file_name: str | None = Header(default=None, alias="x-lyra-file-name"),
        account_id: str | None = Query(default=None, alias="accountId"),
    ) -> Any:
        """接收上传字节，解析并生成导入预览。

        @param request: 请求对象。
        @param x_lyra_file_name: 原始文件名。
        @param account_id: 可选的显式账户 UUID。
        @returns Flutter 可解析的预览结果与一次性确认令牌。
        @raises HTTPException: 上传不合法、格式未实现或无法唯一绑定账户。
        """
        if (
            x_lyra_file_name is None
            or not x_lyra_file_name.strip()
            or len(x_lyra_file_name) > 256
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_ARGUMENT",
                    "message": "上传请求必须携带有效的 x-lyra-file-name 头",
                },
            )
        content = await request.body()
        connection = request.app.state.database.connection

        try:
            # 账户标识来自文件名，只有解析后才知道；而预览需要该账户的账本上下文。
            # 因此顺序是：解析 → 绑定账户 → 读取账本上下文 → 生成预览。
            parsed = parse_uploaded_file(file_name=x_lyra_file_name, content=content)
            resolved = resolve_import_account(
                parsed.batch.account, _active_accounts(connection), account_id
            )
        except ApplicationError as error:
            raise HTTPException(
                status_code=error.status_code,
                detail={
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                },
            ) from error
        except IngestError as error:
            raise HTTPException(
                status_code=422,
                detail={"code": "IMPORT_VALIDATION_FAILED", "message": str(error)},
            ) from error

        base_version, existing = _ledger_context(connection, resolved.account_id)
        now = datetime.now(UTC)
        preview = create_import_preview(CreateImportPreviewInput(
            batch=parsed.batch,
            parser_version=PARSER_VERSION,
            base_ledger_version=base_version,
            existing_transaction_hashes=existing,
        ))

        import_id = str(uuid.uuid4())
        issued = issue_import_preview_authorization(
            import_id, preview.preview_fingerprint, now, PREVIEW_TTL_MILLISECONDS
        )
        store = _store(request)
        try:
            # 上传已完整接收并解析完毕，因此直接从 parsing 建单；随后 record_preview
            # 以 CAS 推进到 preview_ready，那一步同时写入预览元数据与令牌哈希。
            store.create_import_job(
                _job_record(import_id=import_id, parsed=parsed, now=now)
            )
            if not store.record_preview(
                import_id,
                target_account_id=resolved.account_id,
                preview_token_hash=issued.record.token_hash,
                preview_fingerprint=preview.preview_fingerprint,
                preview_expires_at=now
                + timedelta(milliseconds=PREVIEW_TTL_MILLISECONDS),
                # NULL 表示「预览时是空账本」：该列引用 ledger_versions，空账本没有 0 行。
                preview_ledger_version=base_version if base_version > 0 else None,
                total_rows=preview.total_rows,
                inserted_rows=preview.insertable_rows,
                skipped_rows=preview.existing_rows,
                now=now,
            ):
                raise ApplicationError(
                    code="IMPORT_STATE_CONFLICT",
                    message="导入作业状态已变化，请刷新后重试",
                    status_code=409,
                    retryable=False,
                )
        except ApplicationError as error:
            raise HTTPException(
                status_code=error.status_code, detail={"code": error.code, "message": error.message}
            ) from error

        # 提交阶段需要重新解析，而解析结果不持久化，因此这里必须把原始字节暂存。
        StagedUploadStore(request.app.state.settings.lyra_import_staging_dir).stage(
            import_id, content, parsed.file_hash
        )
        return project_import_preview(
            preview,
            import_id=import_id,
            account_id=resolved.account_id,
            base_ledger_version=base_version,
            confirm_token=issued.token,
        )

    @router.get("/v1/imports/{import_id}")
    def read_import_job(import_id: str, request: Request) -> Any:
        """读取导入作业的公开状态，供 App 轮询与恢复流程。"""
        _require_import_id(import_id)
        job = _store(request).load_import_job(import_id)
        if job is None:
            raise _conflict()
        return project_import_job(create_public_import_job(job))

    @router.post("/v1/imports/{import_id}/commit")
    async def commit(
        import_id: str,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Any:
        """确认并提交一个预览过的导入。

        请求体为 ``{"confirmToken": "..."}``；令牌是预览响应里唯一一次返回的明文。

        @param import_id: 作业 ID。
        @param request: 请求对象。
        @returns 提交结果：是否变更、新账本版本与行数。
        @raises HTTPException: 参数无效（400）、作业/授权失效（409/422）。
        """
        _require_import_id(import_id)
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "导入提交参数无效"},
            ) from error
        token = body.get("confirmToken") if isinstance(body, dict) else None
        # **上限与参考实现一致（512）**：没有上限时，超长令牌会一路进入哈希与比较，
        # 而客户端的截断逻辑按那个上限设计。
        if (
            not isinstance(token, str)
            or not token.strip()
            or len(token) > _MAX_CONFIRM_TOKEN_LENGTH
        ):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "导入提交参数无效"},
            )

        connection = request.app.state.database.connection
        store = _store(request)
        job = store.load_import_job(import_id)
        if job is None:
            raise _conflict()
        if (
            job.preview_fingerprint is None
            or job.preview_token_hash is None
            or job.preview_expires_at is None
            or job.target_account_id is None
        ):
            raise _conflict()

        # **幂等必须早于任何会受首次提交影响的步骤。**
        #
        # 首次提交会消费并清理暂存文件；若把幂等检查放在加载暂存之后，重放请求会在
        # 那里就失败于 ``IMPORT_PREVIEW_STALE``——**回放路径永远走不到**，
        # 幂等键形同虚设。
        now = datetime.now(UTC)
        try:
            idempotency_plan = _plan_idempotency(
                connection, device=_device(request), key=idempotency_key,
                request_hash=_request_fingerprint(import_id, token), now=now,
            )
            if isinstance(idempotency_plan, IdempotentMutationReplay):
                replay_job = store.load_import_job(idempotency_plan.result_resource_id)
                if replay_job is None or replay_job.status != "committed":
                    # 键指向的作业不存在或已不相符——回放一个不存在的作业会让客户端
                    # 拿到指向空处的 ID。
                    raise ApplicationError(
                        code="IDEMPOTENCY_KEY_REUSED",
                        message="幂等键已用于其他请求，请生成新的幂等键",
                        status_code=409, retryable=False,
                    )
                return _replay_payload(replay_job)
        except ApplicationError as error:
            raise HTTPException(
                status_code=error.status_code, detail={"code": error.code, "message": error.message}
            ) from error

        stager = StagedUploadStore(request.app.state.settings.lyra_import_staging_dir)
        try:
            content = stager.load(import_id, job.file_hash)
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(
                status_code=409,
                detail={"code": "IMPORT_PREVIEW_STALE", "message": str(error)},
            ) from error

        try:
            parsed = parse_uploaded_file(file_name=job.original_file_name, content=content)
            # 计划必须在事务外算好：去重需要该账户当前的全部交易。
            normalized = normalize_transaction_batch(parsed.batch)
            snapshot = DuckDbLedgerRepository(connection).load_snapshot(
                AccountScope(kind="account", account=normalized.account)
            )
            plan = build_import_commit_plan(
                ImportCommitPreview(
                    account=normalized.account, transactions=normalized.transactions
                ),
                snapshot.transactions,
            )
        except IngestError as error:
            raise HTTPException(
                status_code=422,
                detail={"code": "IMPORT_VALIDATION_FAILED", "message": str(error)},
            ) from error
        except ApplicationError as error:
            raise HTTPException(
                status_code=error.status_code, detail={"code": error.code, "message": error.message}
            ) from error

        repository = DuckDbImportCommitRepository(connection)

        try:
            if isinstance(idempotency_plan, IdempotentMutationStart):
                # **登记在提交之前。** 若等到写锁内部才登记，两个并发同键请求会先后
                # 各拿到锁、各执行一次提交——幂等在最需要它的场景失效。
                start_mutation_idempotency_record(
                    connection, record=idempotency_plan.record,
                    replaces_expired_record_id=idempotency_plan.replaces_expired_record_id,
                )
            result = commit_import_job(
                store=store,
                import_id=import_id,
                preview_record=ImportPreviewAuthorizationRecord(
                    import_id=import_id,
                    preview_fingerprint=job.preview_fingerprint,
                    token_hash=job.preview_token_hash,
                    expires_at=job.preview_expires_at,
                ),
                preview_fingerprint=job.preview_fingerprint,
                token=token,
                plan=plan,
                now=now,
                commit_ledger=lambda version: repository.commit_import(
                    account_id=job.target_account_id,
                    plan=plan,
                    expected_ledger_version=version,
                    # **来源引用用作业 UUID，不是文件名。** 文件名会重复（"Transactions.xlsx"），
                    # 无法据此回溯到具体哪一次导入；作业 ID 是唯一且可审计的。
                    source_reference=import_id,
                    idempotency_completion=(
                        IdempotencyCompletion(
                            record_id=idempotency_plan.record.id,
                            result_resource_id=import_id,
                            response_status_code=200,
                            completed_at=now,
                        )
                        if isinstance(idempotency_plan, IdempotentMutationStart)
                        else None
                    ),
                ),
            )
        except ApplicationError as error:
            raise HTTPException(
                status_code=error.status_code,
                detail={"code": error.code, "details": error.details},
            ) from error
        finally:
            # 无论成败都清理暂存：失败的作业必须重新预览，留着字节没有用途。
            stager.discard(import_id)

        return {
            "changed": result.changed,
            "ledgerVersion": result.ledger_version,
            "insertedRows": result.inserted_rows,
            "skippedRows": result.skipped_rows,
        }

    @router.post("/v1/imports/{import_id}/cancel")
    def cancel(import_id: str, request: Request) -> Any:
        """取消一个尚未开始提交的导入作业。"""
        _require_import_id(import_id)
        try:
            result = cancel_import_job(
                _store(request), import_id, datetime.now(UTC)
            )
        except ApplicationError as error:
            raise HTTPException(
                status_code=error.status_code, detail={"code": error.code, "message": error.message}
            ) from error
        return project_import_job(result)

    return router


def _active_accounts(connection: Any) -> tuple[ImportAccountRecord, ...]:
    """读取全部账户（含已归档）；是否参与绑定由绑定逻辑决定。"""
    return tuple(
        ImportAccountRecord(
            id=str(row[0]), legacy_account_key=row[1], masked_suffix=row[2], status=row[3]
        )
        for row in connection.execute(
            """
            SELECT id, legacy_account_key, masked_suffix, status
            FROM accounts ORDER BY legacy_account_key
            """
        ).fetchall()
    )


def _ledger_context(connection: Any, account_id: str) -> tuple[int, frozenset[str]]:
    """读取目标账户的账本基准版本与已有交易哈希。

    @returns ``(基准版本, 已有哈希)``；空账本的基准版本为 0。
    """
    version_row = connection.execute(
        "SELECT coalesce(max(version), 0) FROM ledger_versions"
    ).fetchone()
    base_version = int(version_row[0]) if version_row is not None else 0
    existing = frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT transaction_hash FROM transactions WHERE account_id = CAST(? AS UUID)",
            [account_id],
        ).fetchall()
    )
    return base_version, existing


def _job_record(*, import_id: str, parsed: Any, now: datetime) -> PersistedImportJobRecord:
    """按上传与解析结果构造初始作业记录。

    状态直接是 ``parsing``：上传已完整接收且解析已完成，没有必要假装经历
    ``uploaded`` 或 ``parsing`` 的更早阶段。
    """
    stamp = _iso(now)
    return PersistedImportJobRecord(
        id=import_id,
        file_hash=parsed.file_hash,
        original_file_name=parsed.file_name,
        file_type=parsed.file_type,
        file_size_bytes=parsed.size_bytes,
        parser_version=PARSER_VERSION,
        status="parsing",
        target_account_id=None,
        preview_token_hash=None,
        preview_ledger_version=None,
        total_rows=None,
        inserted_rows=None,
        skipped_rows=None,
        error_code=None,
        error_summary=None,
        committed_ledger_version=None,
        created_at=stamp,
        updated_at=stamp,
    )


def _iso(value: datetime) -> str:
    """渲染为与 JavaScript ``toISOString()`` 同形的文本。"""
    return (
        value.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _device(request: Request) -> Any:
    """取出已认证设备。"""
    return getattr(request.state, "device", None)


def _request_fingerprint(import_id: str, token: str) -> str:
    """本次提交的请求指纹。

    用「作业 ID + 令牌哈希」而非明文令牌：指纹会入库，**而令牌是唯一一次返回的明文**，
    把它写进数据库等于长期保留一个可用凭据。
    """
    import hashlib

    return hashlib.sha256(f"{import_id}:{token}".encode()).hexdigest()


def _plan_idempotency(
    connection: Any, *, device: Any, key: str | None, request_hash: str, now: datetime
) -> Any:
    """按幂等键生成计划；没有键时返回 ``None``。

    无键的请求**不写任何幂等记录**——那是客户端的显式选择，不该由服务端代为登记。
    """
    if key is None or device is None:
        return None
    # 委托共享校验（见 `idempotent_mutation.validate_idempotency_key`）——
    # 另写一份会让"键是否合法"存在两个判断，迟早分叉。
    validate_idempotency_key(key)
    existing = load_mutation_idempotency_record(
        connection, device_id=device.id, operation=IDEMPOTENCY_OPERATION, key_hash=hash_idempotency_key(key)
    )
    return plan_idempotent_mutation(
        record_id=str(uuid.uuid4()), device_id=device.id,
        operation=IDEMPOTENCY_OPERATION, key=key, request_hash=request_hash,
        existing_record=existing, now=now,
    )


def _replay_payload(job: Any) -> Any:
    """把已提交的作业投影为**与正常提交相同的响应形状**。

    **回放必须返回提交结果，而不是作业载荷。** 首次提交返回的是
    ``{changed, ledgerVersion, insertedRows, skippedRows}``；若回放返回作业对象，
    客户端会在重试时拿到一个字段完全不同的响应，而它的解析逻辑只认前者。

    这与参考实现一致：回放路径从作业里**反推出**提交结果的四个字段。
    """
    public = create_public_import_job(job)
    inserted = public.inserted_rows or 0
    return {
        "changed": inserted > 0,
        # 已提交的作业必有 committed_ledger_version；缺失时退回基准版本而不是报错，
        # 与参考实现的 `?? baseLedgerVersion ?? 0` 一致。
        "ledgerVersion": (
            public.committed_ledger_version
            if public.committed_ledger_version is not None
            else (public.base_ledger_version or 0)
        ),
        "insertedRows": inserted,
        "skippedRows": public.skipped_rows or 0,
    }


def _require_import_id(import_id: str) -> None:
    """校验路径里的导入作业 ID 是 UUID。

    **格式非法返回 400 而不是 404**：前者说"你的请求构造错了"，后者说"这个作业不存在"
    ——客户端对两者的处理完全不同（改代码 vs 刷新列表）。参考实现对路径参数用同一个
    schema 校验。

    @param import_id: 路径参数。
    @raises HTTPException: 格式非法（400）。
    """
    if not _UUID_PATTERN.fullmatch(import_id):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_ARGUMENT", "message": "导入作业 ID 格式非法"},
        )


def _conflict() -> HTTPException:
    """统一的作业冲突响应。

    不区分「作业不存在」与「作业不属于当前设备」——否则可以靠状态码探测作业是否存在。
    """
    return HTTPException(status_code=409, detail={"code": "IMPORT_STATE_CONFLICT"})
