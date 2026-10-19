"""Gmail 同步错误映射测试。

一处值得记录的行为：``GMAIL_CONNECTION_ERROR`` 在同步层是可重试的（网络抖动、5xx），
但到了这一层被硬编码成 **409 不可重试**。这会让客户端对一次真正暂时的故障不再重试。

**这是参考实现的既有行为，本模块照实镜像**——迁移的目标是行为一致。
"""

from __future__ import annotations

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.gmail_sync_errors import completion_state, map_sync_error
from lyra.domain.gmail.sync_plan import GmailSyncError

# --------------------------------------------------------------------------
# 已知同步错误码
# --------------------------------------------------------------------------


def test_resync_confirmation_maps_to_409() -> None:
    """缺少重扫确认 → 409，用户需要给出起始日期。"""
    error = map_sync_error(GmailSyncError("GMAIL_RESYNC_CONFIRMATION_REQUIRED", "x"))

    assert error.code == "GMAIL_RESYNC_CONFIRMATION_REQUIRED"
    assert error.status_code == 409
    assert error.retryable is False
    assert "起始日期" in error.message


def test_auth_revoked_maps_to_401() -> None:
    """**授权失效是 401，不是 409。**

    客户端据此触发重新登录流程；报 409 会让它以为只是状态冲突。
    """
    error = map_sync_error(GmailSyncError("GMAIL_AUTH_REVOKED", "x"))

    assert error.status_code == 401
    assert error.retryable is False
    assert "重新授权" in error.message


def test_history_expired_maps_to_409() -> None:
    """游标过期 → 409，用户需要确认重扫范围。"""
    error = map_sync_error(GmailSyncError("GMAIL_HISTORY_EXPIRED", "x"))

    assert error.code == "GMAIL_HISTORY_EXPIRED"
    assert error.status_code == 409
    assert error.retryable is False


# --------------------------------------------------------------------------
# 其余同步错误码
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code", ["GMAIL_CONNECTION_ERROR", "GMAIL_INCREMENTAL_LIMIT_EXCEEDED"]
)
def test_other_sync_codes_become_a_non_retryable_409(code: str) -> None:
    """其余同步错误码统一为 409 不可重试。

    ``GMAIL_INCREMENTAL_LIMIT_EXCEEDED`` 这样处理是对的（必须重扫）；
    ``GMAIL_CONNECTION_ERROR`` 则**丢掉了它的可重试性**——这是参考实现的行为，
    照实镜像。
    """
    error = map_sync_error(GmailSyncError(code, "x", retryable=True))

    assert error.code == "GMAIL_CONNECTION_ERROR"
    assert error.status_code == 409
    # 注意：即使上游标了 retryable=True，这里仍是 False。
    assert error.retryable is False


def test_transient_connection_errors_lose_their_retryability() -> None:
    """**明确记录这一行为**：一次 5xx 导致的可重试失败，出口处变成不可重试。"""
    transient = GmailSyncError(
        "GMAIL_CONNECTION_ERROR", "无法刷新 Gmail 授权，请稍后重试", retryable=True
    )
    assert transient.retryable is True

    mapped = map_sync_error(transient)
    assert mapped.retryable is False


# --------------------------------------------------------------------------
# 透传与兜底
# --------------------------------------------------------------------------


def test_application_errors_pass_through_unchanged() -> None:
    """**已是应用错误时原样透传**，避免丢失更精确的语义。"""
    original = ApplicationError(
        code="LEDGER_VERSION_CONFLICT", message="版本冲突",
        status_code=409, retryable=False,
    )

    assert map_sync_error(original) is original


def test_unknown_errors_become_a_retryable_503() -> None:
    """未知异常 → 503 可重试。"""
    error = map_sync_error(RuntimeError("boom"))

    assert error.code == "GMAIL_CONNECTION_ERROR"
    assert error.status_code == 503
    assert error.retryable is True


def test_unknown_errors_do_not_leak_their_message() -> None:
    """未知异常的原文不外泄——它可能含端点或凭据片段。"""
    error = map_sync_error(RuntimeError("postgres://user:pw@host"))

    assert "postgres" not in error.message
    assert "pw" not in error.message


# --------------------------------------------------------------------------
# 收尾状态
# --------------------------------------------------------------------------


def test_completion_state_advances_the_cursor() -> None:
    """收尾状态为 active 并推进游标。"""
    status, history_id, last_sync_at = completion_state(
        "12345", "2026-10-12T09:00:00.000Z"
    )

    assert status == "active"
    assert history_id == "12345"
    assert last_sync_at == "2026-10-12T09:00:00.000Z"


def test_completion_state_validates_the_cursor() -> None:
    """游标格式非法必须报错，而不是写进连接。"""
    with pytest.raises(Exception):
        completion_state("not-a-cursor", "2026-10-12T09:00:00.000Z")


def test_completion_state_validates_the_timestamp() -> None:
    """时间必须带 UTC 标记。"""
    with pytest.raises(Exception):
        completion_state("12345", "2026-10-12T09:00:00+00:00")


def test_completion_uses_a_synthetic_active_state() -> None:
    """**收尾传的是合成的 active 状态**，因此"已断开不能完成同步"的守卫不会触发。

    走到收尾说明连接既不是断开也不是错误（否则早就返回了）。照实镜像这一顺序。
    """
    # 能完成即证明合成状态生效。
    assert completion_state("1", "2026-10-12T09:00:00.000Z")[0] == "active"
