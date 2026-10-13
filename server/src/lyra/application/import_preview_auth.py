"""导入预览的确认授权。

镜像 TypeScript 版 ``import-preview-auth.ts``。

预览阶段签发的**不透明令牌只返回一次**，服务端只存哈希。提交时必须同时满足五项
条件才放行：作业 ID、预览指纹、作业状态、有效期、令牌本身。

**单项校验不足以提供一次性语义**：本函数只做校验，调用方还必须以 CAS 把作业从
``preview_ready`` 推进到 ``committing``——那一步才是真正的一次性消费。只调本函数
然后直接写账本，会让同一个令牌被并发提交两次。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..security.device_auth import create_opaque_credential, verify_opaque_credential
from .app_error import ApplicationError
from .import_job import ImportJobStatus

_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class ImportPreviewAuthorizationRecord:
    """预览确认令牌对应的服务端持久化记录，不含明文令牌。"""

    import_id: str
    preview_fingerprint: str
    token_hash: str
    expires_at: str


@dataclass(frozen=True)
class IssuedImportPreviewAuthorization:
    """创建预览确认令牌时唯一一次返回给 App 的明文与持久化记录。"""

    token: str
    record: ImportPreviewAuthorizationRecord


def issue_import_preview_authorization(
    import_id: str,
    preview_fingerprint: str,
    now: datetime,
    ttl_milliseconds: int,
) -> IssuedImportPreviewAuthorization:
    """为已经完整验证的导入预览签发短时不透明确认令牌。

    @param import_id: 当前导入作业 ID。
    @param preview_fingerprint: 绑定文件、解析器、账户、账本版本和交易哈希的指纹。
    @param now: 服务端可信时钟。
    @param ttl_milliseconds: 预览可确认的有效期毫秒数。
    @returns 明文令牌与仅含哈希的持久化记录。
    @raises ValueError: 作业 ID、指纹、有效期或时间非法。
    """
    if import_id.strip() == "" or not _FINGERPRINT.fullmatch(preview_fingerprint):
        raise ValueError("导入 ID 或预览指纹无效")
    if (
        not isinstance(ttl_milliseconds, int)
        or isinstance(ttl_milliseconds, bool)
        or ttl_milliseconds <= 0
    ):
        raise ValueError("导入预览有效期必须是正安全整数毫秒数")
    if not _is_aware(now):
        raise ValueError("导入预览签发时间必须带时区")

    credential = create_opaque_credential()
    return IssuedImportPreviewAuthorization(
        token=credential.token,
        record=ImportPreviewAuthorizationRecord(
            import_id=import_id,
            preview_fingerprint=preview_fingerprint,
            token_hash=credential.token_hash,
            expires_at=_iso(now + timedelta(milliseconds=ttl_milliseconds)),
        ),
    )


def assert_import_preview_authorized(
    *,
    record: ImportPreviewAuthorizationRecord,
    import_id: str,
    preview_fingerprint: str,
    token: str,
    job_status: ImportJobStatus,
    now: datetime,
) -> None:
    """在提交事务开始前验证作业状态、预览指纹、有效期和不透明令牌。

    **一次性列出全部失效原因**，而不是遇到第一个就返回：用户与日志都需要知道到底
    哪些条件变了，只报一个会让排查反复碰壁。

    @param record: 签发的授权记录。
    @param import_id: 本次提交的作业 ID。
    @param preview_fingerprint: 本次提交携带的预览指纹。
    @param token: App 提交的明文令牌。
    @param job_status: 当前作业状态。
    @param now: 服务端可信时间。
    @raises ApplicationError: 任一条件失效（409），调用方必须要求重新预览。
    """
    changed_fields: list[str] = []
    if record.import_id != import_id:
        changed_fields.append("import_id")
    if record.preview_fingerprint != preview_fingerprint:
        changed_fields.append("preview_fingerprint")
    if job_status != "preview_ready":
        changed_fields.append("job_status")
    if not _is_aware(now) or _ms(now) >= _ms(_parse(record.expires_at)):
        changed_fields.append("expires_at")
    # 令牌用常量时间比较，避免通过响应耗时逐字节试探。
    if not verify_opaque_credential(token, record.token_hash):
        changed_fields.append("preview_token")

    if changed_fields:
        raise ApplicationError(
            code="IMPORT_PREVIEW_STALE",
            message="导入预览授权已失效，必须重新预览",
            status_code=409,
            retryable=False,
            details={"changed_fields": changed_fields},
        )


def _is_aware(value: object) -> bool:
    """判断是否为带时区的 ``datetime``。"""
    return isinstance(value, datetime) and value.tzinfo is not None


def _parse(value: str) -> datetime:
    """解析 ISO 时间戳。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _ms(value: datetime) -> float:
    """折算为毫秒。"""
    return value.timestamp() * 1000


def _iso(value: datetime) -> str:
    """渲染为与 JavaScript ``toISOString()`` 同形的文本。"""
    return (
        value.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
