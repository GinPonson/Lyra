"""导入预览授权测试。

这是提交阶段的安全闸门，因此每条失效路径都必须有独立用例。三处最要紧的：

1. **令牌比对必须是常量时间**，且用错的令牌与过期的令牌都要被拒；
2. **一次性列出全部失效原因**，而不是只报第一个；
3. **授权校验本身不提供一次性语义** —— 真正的一次性由调用方的 CAS 完成，
   这条写进了模块文档，也在断言里体现（同一记录校验两次都会通过）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.import_preview_auth import (
    ImportPreviewAuthorizationRecord,
    assert_import_preview_authorized,
    issue_import_preview_authorization,
)
from lyra.security.device_auth import hash_opaque_credential

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
TTL_MS = 15 * 60 * 1000
IMPORT_ID = "44444444-4444-4444-4444-444444444444"
FINGERPRINT = "a" * 64


def _issued():
    """签发一份授权。"""
    return issue_import_preview_authorization(IMPORT_ID, FINGERPRINT, NOW, TTL_MS)


def _assert(
    *,
    record: ImportPreviewAuthorizationRecord,
    token: str,
    import_id: str = IMPORT_ID,
    fingerprint: str = FINGERPRINT,
    job_status: str = "preview_ready",
    now: datetime = NOW,
) -> None:
    """以默认条件调用校验，可覆写任意一项。"""
    assert_import_preview_authorized(
        record=record,
        import_id=import_id,
        preview_fingerprint=fingerprint,
        token=token,
        job_status=job_status,  # type: ignore[arg-type]
        now=now,
    )


# --------------------------------------------------------------------------
# 签发
# --------------------------------------------------------------------------


def test_issue_stores_only_the_token_hash() -> None:
    """持久化记录里绝不能出现明文令牌。"""
    issued = _issued()

    assert issued.record.token_hash == hash_opaque_credential(issued.token)
    assert issued.token not in repr(issued.record)
    assert issued.record.import_id == IMPORT_ID
    assert issued.record.preview_fingerprint == FINGERPRINT
    assert issued.record.expires_at == "2026-10-12T09:15:00.000Z"


def test_issued_tokens_are_unique() -> None:
    """两次签发不得得到同一令牌。"""
    assert _issued().token != _issued().token


@pytest.mark.parametrize(
    ("import_id", "fingerprint", "ttl"),
    [
        ("", FINGERPRINT, TTL_MS),
        ("   ", FINGERPRINT, TTL_MS),
        (IMPORT_ID, "not-a-hash", TTL_MS),
        (IMPORT_ID, "A" * 64, TTL_MS),
        (IMPORT_ID, FINGERPRINT, 0),
        (IMPORT_ID, FINGERPRINT, -1),
        (IMPORT_ID, FINGERPRINT, 1.5),
        (IMPORT_ID, FINGERPRINT, True),
    ],
)
def test_issue_rejects_invalid_input(import_id: str, fingerprint: str, ttl: object) -> None:
    """作业 ID、指纹与有效期都必须合法。"""
    with pytest.raises(ValueError):
        issue_import_preview_authorization(
            import_id, fingerprint, NOW, ttl  # type: ignore[arg-type]
        )


def test_issue_rejects_naive_datetime() -> None:
    """朴素时间必须拒绝：有效期会带本地时区偏移。"""
    with pytest.raises(ValueError, match="必须带时区"):
        issue_import_preview_authorization(
            IMPORT_ID, FINGERPRINT, datetime(2026, 10, 12, 9, 0, 0), TTL_MS
        )


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


def test_authorized_when_everything_matches() -> None:
    """五项全部相符时放行。"""
    issued = _issued()
    _assert(record=issued.record, token=issued.token)


def test_rejects_wrong_token() -> None:
    """错误令牌必须拒绝。"""
    issued = _issued()
    with pytest.raises(ApplicationError) as raised:
        _assert(record=issued.record, token="wrong-token")

    assert raised.value.code == "IMPORT_PREVIEW_STALE"
    assert raised.value.details == {"changed_fields": ["preview_token"]}


def test_rejects_expired_authorization() -> None:
    """超过有效期的授权必须拒绝。"""
    issued = _issued()
    at_expiry = NOW + timedelta(milliseconds=TTL_MS)

    with pytest.raises(ApplicationError) as raised:
        _assert(record=issued.record, token=issued.token, now=at_expiry)

    assert raised.value.details == {"changed_fields": ["expires_at"]}

    # 差一毫秒仍在有效期内。
    _assert(
        record=issued.record,
        token=issued.token,
        now=at_expiry - timedelta(milliseconds=1),
    )


def test_rejects_wrong_job_status() -> None:
    """作业不在 preview_ready 时必须拒绝。"""
    issued = _issued()
    for status in ("uploaded", "parsing", "committing", "committed", "cancelled"):
        with pytest.raises(ApplicationError) as raised:
            _assert(record=issued.record, token=issued.token, job_status=status)
        assert raised.value.details == {"changed_fields": ["job_status"]}


def test_rejects_mismatched_fingerprint_and_import_id() -> None:
    """指纹或作业 ID 不符必须拒绝。"""
    issued = _issued()

    with pytest.raises(ApplicationError) as by_fingerprint:
        _assert(record=issued.record, token=issued.token, fingerprint="b" * 64)
    assert by_fingerprint.value.details == {"changed_fields": ["preview_fingerprint"]}

    with pytest.raises(ApplicationError) as by_import:
        _assert(record=issued.record, token=issued.token, import_id="other")
    assert by_import.value.details == {"changed_fields": ["import_id"]}


def test_reports_every_failure_at_once() -> None:
    """**一次性列出全部失效原因**，而不是只报第一个。

    只报一个会让排查反复碰壁：修好一个又冒出下一个。
    """
    issued = _issued()

    with pytest.raises(ApplicationError) as raised:
        _assert(
            record=issued.record,
            token="wrong-token",
            import_id="other",
            fingerprint="b" * 64,
            job_status="committed",
            now=NOW + timedelta(hours=1),
        )

    assert raised.value.details == {
        "changed_fields": [
            "import_id",
            "preview_fingerprint",
            "job_status",
            "expires_at",
            "preview_token",
        ]
    }


def test_naive_now_counts_as_expiry_failure() -> None:
    """朴素时间视为校验失败，而不是静默通过。"""
    issued = _issued()
    with pytest.raises(ApplicationError) as raised:
        _assert(
            record=issued.record,
            token=issued.token,
            now=datetime(2026, 10, 12, 9, 0, 0),
        )
    assert raised.value.details == {"changed_fields": ["expires_at"]}


def test_validation_alone_does_not_consume_the_token() -> None:
    """**授权校验本身不提供一次性语义**——同一记录校验两次都会通过。

    一次性由调用方的 CAS（preview_ready → committing）负责。这条断言把职责边界
    写在测试里：如果哪天有人以为"校验过了就不可能重复提交"，这里会提醒他。
    """
    issued = _issued()

    _assert(record=issued.record, token=issued.token)
    _assert(record=issued.record, token=issued.token)
