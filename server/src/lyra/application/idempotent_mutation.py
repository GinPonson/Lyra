"""幂等修改的判定与完成。

镜像 TypeScript 版 ``idempotent-mutation.ts``。

App 重试是常态（网络抖动、超时重发），因此**同一个幂等键必须得到同一个结果**，
而不是把修改做两遍。判定集中在 :func:`plan_idempotent_mutation`：

| 已有记录 | 结果 |
| --- | --- |
| 无 | 开始新记录（``start``） |
| 未过期、请求指纹相同、进行中 | 409 且可重试（``IDEMPOTENCY_REQUEST_IN_PROGRESS``） |
| 未过期、请求指纹相同、已完成 | 重放（``replay``）——**不重复执行修改** |
| 未过期、请求指纹**不同** | 409 且不可重试（``IDEMPOTENCY_KEY_REUSED``） |
| 已过期 | 开始新记录，并原子替换旧记录 |

**只保存资源 ID 与 HTTP 状态**，不保存响应正文、access token、refresh token 或
OAuth code——幂等表的用途是"重建一个当前的安全响应"，不是缓存响应体。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from .app_error import ApplicationError

# 幂等记录默认保留 24 小时。
DEFAULT_IDEMPOTENCY_TTL_HOURS = 24

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class MutationIdempotencyRecord:
    """与 ``mutation_idempotency_records`` 表对齐，不含明文幂等键或响应正文。"""

    id: str
    device_id: str
    operation: str
    key_hash: str
    request_hash: str
    status: Literal["in_progress", "completed"]
    result_resource_id: str | None
    response_status_code: int | None
    created_at: str
    expires_at: str
    completed_at: str | None


@dataclass(frozen=True)
class IdempotentMutationReplay:
    """命中已完成记录：调用方应直接返回该结果，不再执行修改。"""

    kind: Literal["replay"]
    record_id: str
    result_resource_id: str
    response_status_code: int
    completed_at: str


@dataclass(frozen=True)
class IdempotentMutationStart:
    """需要真正执行修改。"""

    kind: Literal["start"]
    replaces_expired_record_id: str | None
    record: MutationIdempotencyRecord


IdempotentMutationPlan = IdempotentMutationReplay | IdempotentMutationStart


def hash_idempotency_key(key: str) -> str:
    """计算幂等键的 SHA-256；**明文键永不入库**。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def plan_idempotent_mutation(
    *,
    record_id: str,
    device_id: str,
    operation: str,
    key: str,
    request_hash: str,
    existing_record: MutationIdempotencyRecord | None,
    now: datetime,
    ttl_hours: int = DEFAULT_IDEMPOTENCY_TTL_HOURS,
) -> IdempotentMutationPlan:
    """判定一次修改请求应该新建记录、重放还是拒绝。

    @param record_id: 新记录的 ID（新建时使用）。
    @param device_id: 发起设备；幂等键按设备作用域隔离。
    @param operation: 操作名，长度须大于 1。
    @param key: 客户端提交的明文幂等键。
    @param request_hash: 请求体的 SHA-256，用于识别"同键不同请求"。
    @param existing_record: 按设备+操作+键哈希查得的既有记录。
    @param now: 服务端可信时间。
    @param ttl_hours: 新记录保留小时数。
    @returns 重放结果或新建计划。
    @raises ValueError: 任一字段格式非法。
    @raises ApplicationError: 键被复用于不同请求（409），或同请求仍在处理中（409）。
    """
    resolved_id = _require_text(record_id, "幂等记录 ID")
    resolved_device = _require_text(device_id, "幂等设备 ID")
    resolved_operation = _require_operation(operation)
    key_hash = hash_idempotency_key(key)
    resolved_request_hash = _require_sha256(request_hash, "幂等请求指纹")
    _require_aware(now, "幂等请求时间")
    if not isinstance(ttl_hours, int) or isinstance(ttl_hours, bool) or ttl_hours <= 0:
        raise ValueError("幂等记录保留小时数必须是正安全整数")

    existing = existing_record
    if existing is not None:
        _assert_matching(existing, resolved_device, resolved_operation, key_hash)
        expires_at = _parse_timestamp(existing.expires_at, "幂等记录过期时间")
        if expires_at > now:
            if existing.request_hash != resolved_request_hash:
                # 同键不同请求：客户端复用键是 bug，必须让调用方显式换键。
                raise ApplicationError(
                    code="IDEMPOTENCY_KEY_REUSED",
                    message="幂等键已用于其他请求，请生成新的幂等键",
                    status_code=409,
                    retryable=False,
                )
            if existing.status == "in_progress":
                raise ApplicationError(
                    code="IDEMPOTENCY_REQUEST_IN_PROGRESS",
                    message="相同修改请求仍在处理中，请稍后重试",
                    status_code=409,
                    retryable=True,
                )
            if (
                existing.result_resource_id is None
                or existing.response_status_code is None
                or existing.completed_at is None
            ):
                raise ValueError("已完成幂等记录缺少结果引用")
            return IdempotentMutationReplay(
                kind="replay",
                record_id=existing.id,
                result_resource_id=existing.result_resource_id,
                response_status_code=existing.response_status_code,
                completed_at=existing.completed_at,
            )

    created_at = _iso(now)
    return IdempotentMutationStart(
        kind="start",
        replaces_expired_record_id=existing.id if existing is not None else None,
        record=MutationIdempotencyRecord(
            id=resolved_id,
            device_id=resolved_device,
            operation=resolved_operation,
            key_hash=key_hash,
            request_hash=resolved_request_hash,
            status="in_progress",
            result_resource_id=None,
            response_status_code=None,
            created_at=created_at,
            expires_at=_iso(now + timedelta(hours=ttl_hours)),
            completed_at=None,
        ),
    )


def complete_idempotent_mutation(
    record: MutationIdempotencyRecord,
    *,
    result_resource_id: str,
    response_status_code: int,
    now: datetime,
) -> MutationIdempotencyRecord:
    """在业务写入成功的**同一事务内**完成幂等记录。

    只保存可重新读取安全响应所需的资源 ID 与 HTTP 状态。

    @param record: 进行中的幂等记录。
    @param result_resource_id: 业务结果资源 ID。
    @param response_status_code: HTTP 状态，必须是 2xx。
    @param now: 完成时间。
    @returns 已完成的新记录。
    @raises ValueError: 资源 ID 为空或状态码非 2xx。
    @raises ApplicationError: 记录不处于进行中（说明已重复完成）。
    """
    if record.status != "in_progress":
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REUSED",
            message="幂等修改已经完成",
            status_code=409,
            retryable=False,
        )
    resolved_resource = _require_text(result_resource_id, "幂等结果资源 ID")
    if (
        not isinstance(response_status_code, int)
        or isinstance(response_status_code, bool)
        or not 200 <= response_status_code <= 299
    ):
        raise ValueError("幂等结果 HTTP 状态必须是 2xx 整数")
    _require_aware(now, "幂等完成时间")
    return replace(
        record,
        status="completed",
        result_resource_id=resolved_resource,
        response_status_code=response_status_code,
        completed_at=_iso(now),
    )


def _assert_matching(
    record: MutationIdempotencyRecord,
    device_id: str,
    operation: str,
    key_hash: str,
) -> None:
    """确认记录确实属于同一设备、操作与键哈希。

    @raises ValueError: 记录与查询条件不一致——说明查询或索引已经出错。
    """
    if (
        record.device_id != device_id
        or record.operation != operation
        or record.key_hash != key_hash
    ):
        raise ValueError("幂等记录与查询条件不匹配")


def _require_text(value: str, label: str) -> str:
    """要求非空白文本并去除两端空白。"""
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{label}不能为空")
    return normalized


def _require_operation(value: str) -> str:
    """操作名长度必须大于 1，与表的 CHECK 约束一致。"""
    normalized = _require_text(value, "幂等操作名")
    if len(normalized) <= 1:
        raise ValueError("幂等操作名长度必须大于 1")
    return normalized


def _require_sha256(value: str, label: str) -> str:
    """要求 64 位小写十六进制指纹。"""
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{label}必须是 64 位小写十六进制")
    return value


def _require_aware(value: datetime, label: str) -> datetime:
    """要求带时区的时间；朴素时间会在比较时静默按本地时区解释。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label}必须带时区")
    return value


def _parse_timestamp(value: str, label: str) -> datetime:
    """解析 ISO 时间戳（``Z`` 或 ``+00:00`` 均可）。

    @raises ValueError: 无法解析。
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label}格式非法") from error
    return _require_aware(parsed, label)


def _iso(value: datetime) -> str:
    """渲染为与 JavaScript ``toISOString()`` 同形的文本。"""
    return (
        value.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# 明文幂等键的长度与字符集，与参考实现的 `^[A-Za-z0-9._:-]{16,200}$` 一致。
MIN_IDEMPOTENCY_KEY_LENGTH = 16
MAX_IDEMPOTENCY_KEY_LENGTH = 200


def validate_idempotency_key(key: str) -> None:
    """校验明文幂等键的长度与字符集。

    **键会进入日志与数据库**，因此只允许安全字符；**长度是协议的一部分**——不检查会
    让客户端以为短键可用，而切换后同样的键会被拒。

    @param key: 明文幂等键。
    @raises ApplicationError: 长度或字符集不符（400）。
    """
    if not (
        MIN_IDEMPOTENCY_KEY_LENGTH <= len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH
        and key.isascii()
        and all(character.isalnum() or character in "._:-" for character in key)
    ):
        raise ApplicationError(
            code="INVALID_ARGUMENT",
            message="幂等键必须是 16 至 200 位安全字符",
            status_code=400,
            retryable=False,
        )
