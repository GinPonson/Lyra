"""Gmail 同步计划测试。

两处"必须停下来问用户"的设计：

1. **首次扫描与重扫需要用户选择全部邮件或起始日期**——与 CSV 独立；
2. **增量响应被截断时判为需要重扫**——照常推进游标会**静默漏掉**中间的消息。
"""

from __future__ import annotations

import pytest

from lyra.domain.gmail.sync_plan import (
    GMAIL_SYNC_ERROR_CODES,
    GmailIncrementalFetchObservation,
    GmailSyncError,
    GmailSyncState,
    complete_gmail_sync,
    decide_gmail_incremental_fetch,
    is_gmail_sync_error_code,
    mark_gmail_history_expired,
    plan_gmail_sync,
)


def _state(status: str, history_id: str | None = "42", **overrides) -> GmailSyncState:
    base = {"status": status, "history_id": history_id, "last_sync_at": None, "last_error_code": None}
    base.update(overrides)
    return GmailSyncState(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 计划
# --------------------------------------------------------------------------


def test_active_connection_uses_incremental() -> None:
    """有游标的 active 连接走增量。"""
    request = plan_gmail_sync(_state("active"))

    assert request.mode == "incremental"
    assert request.start_history_id == "42"  # type: ignore[union-attr]


def test_active_without_a_cursor_requires_a_rescan_range() -> None:
    """active 但没有游标时报游标失效。"""
    with pytest.raises(GmailSyncError) as raised:
        plan_gmail_sync(_state("active", history_id=None))

    assert raised.value.code == "GMAIL_HISTORY_EXPIRED"


@pytest.mark.parametrize("status", ["pending_initial_scan", "needs_resync"])
def test_first_scan_and_rescan_require_user_confirmation(status: str) -> None:
    """缺少明确范围时不默认全量，也不从已有 CSV 代选日期。"""
    with pytest.raises(GmailSyncError) as raised:
        plan_gmail_sync(_state(status, history_id=None))

    assert raised.value.code == "GMAIL_RESYNC_CONFIRMATION_REQUIRED"


@pytest.mark.parametrize("status", ["pending_initial_scan", "needs_resync"])
def test_confirmed_range_yields_a_bounded_rescan(status: str) -> None:
    """给出确认日期后返回有界重扫。"""
    request = plan_gmail_sync(
        _state(status, history_id=None), confirmed_rescan_after_date="2026-01-05"
    )

    assert request.mode == "bounded_rescan"
    assert request.after_date == "2026-01-05"  # type: ignore[union-attr]


@pytest.mark.parametrize("status", ["pending_initial_scan", "needs_resync", "active"])
def test_explicit_all_mail_is_independent_of_any_csv_dates(status: str) -> None:
    """显式选择全量时不注入任何日期下界，仍使用有界消息读取。"""
    request = plan_gmail_sync(_state(status), scan_all=True)
    assert request.mode == "bounded_rescan"
    assert request.after_date is None  # type: ignore[union-attr]


def test_all_mail_and_custom_date_are_mutually_exclusive() -> None:
    """拒绝含义冲突的范围，不能悄悄取其中一个。"""
    with pytest.raises(ValueError):
        plan_gmail_sync(
            _state("pending_initial_scan"), scan_all=True,
            confirmed_rescan_after_date="2026-01-05",
        )


def test_disconnected_connection_reports_auth_revoked() -> None:
    """已断开的连接报需要重新授权，而不是笼统的连接错误。

    两者的处置方式不同：前者要用户重新走一遍 OAuth，后者是排查连接故障。
    """
    with pytest.raises(GmailSyncError) as raised:
        plan_gmail_sync(_state("disconnected", history_id=None))

    assert raised.value.code == "GMAIL_AUTH_REVOKED"


def test_error_status_reports_a_connection_error() -> None:
    """错误状态报连接错误。"""
    with pytest.raises(GmailSyncError) as raised:
        plan_gmail_sync(_state("error", history_id=None))

    assert raised.value.code == "GMAIL_CONNECTION_ERROR"


# --------------------------------------------------------------------------
# 增量结果判定
# --------------------------------------------------------------------------


def test_complete_fetch_is_ready() -> None:
    """未截断时给出完成游标。"""
    decision = decide_gmail_incremental_fetch(
        _state("active"),
        GmailIncrementalFetchObservation(
            observed_history_id="99", candidate_message_count=3, truncated=False
        ),
    )

    assert decision.kind == "ready"
    assert decision.completion_history_id == "99"  # type: ignore[union-attr]
    assert decision.candidate_message_count == 3  # type: ignore[union-attr]


def test_truncated_fetch_forces_a_resync() -> None:
    """**截断必须判为需要重扫。**

    截断意味着增量窗口不完整；照常推进游标会**静默漏掉**中间的消息，
    而那些交易本应进入账本。
    """
    decision = decide_gmail_incremental_fetch(
        _state("active"),
        GmailIncrementalFetchObservation(
            observed_history_id="99", candidate_message_count=500, truncated=True
        ),
    )

    assert decision.kind == "needs_resync"
    assert decision.error_code == "GMAIL_INCREMENTAL_LIMIT_EXCEEDED"  # type: ignore[union-attr]
    assert decision.state.status == "needs_resync"  # type: ignore[union-attr]
    assert decision.state.last_error_code == "GMAIL_INCREMENTAL_LIMIT_EXCEEDED"  # type: ignore[union-attr]


def test_zero_candidates_is_a_valid_ready_result() -> None:
    """零条候选是合法结果，不是错误——这段时间邮箱没有新邮件。"""
    decision = decide_gmail_incremental_fetch(
        _state("active"),
        GmailIncrementalFetchObservation(
            observed_history_id="99", candidate_message_count=0, truncated=False
        ),
    )

    assert decision.kind == "ready"


@pytest.mark.parametrize("status", ["disconnected", "needs_resync", "error"])
def test_non_active_connections_cannot_apply_incremental_results(status: str) -> None:
    """非 active 连接不能处理增量结果。"""
    with pytest.raises(GmailSyncError) as raised:
        decide_gmail_incremental_fetch(
            _state(status),
            GmailIncrementalFetchObservation(
                observed_history_id="99", candidate_message_count=1, truncated=False
            ),
        )

    assert raised.value.code == "GMAIL_CONNECTION_ERROR"


@pytest.mark.parametrize("count", [-1, 1.5, True])
def test_invalid_candidate_counts_are_rejected(count: object) -> None:
    """候选消息数必须是非负整数。"""
    with pytest.raises(ValueError, match="非负安全整数"):
        decide_gmail_incremental_fetch(
            _state("active"),
            GmailIncrementalFetchObservation(
                observed_history_id="99", candidate_message_count=count, truncated=False  # type: ignore[arg-type]
            ),
        )


# --------------------------------------------------------------------------
# 游标失效与完成
# --------------------------------------------------------------------------


def test_marks_history_expired_only_from_active() -> None:
    """只有 active 连接可以标记游标失效。"""
    updated = mark_gmail_history_expired(_state("active"), "GMAIL_HISTORY_EXPIRED")

    assert updated.status == "needs_resync"
    assert updated.last_error_code == "GMAIL_HISTORY_EXPIRED"
    # 游标保留：重扫的起始日期由用户确认，但旧游标仍是审计信息。
    assert updated.history_id == "42"


def test_history_expiry_rejects_other_error_codes() -> None:
    """**只允许记录 ``GMAIL_HISTORY_EXPIRED``。**

    其它错误码对应不同的处置方式，用它们表示"游标失效"会让调用方走错分支。
    """
    with pytest.raises(ValueError, match="只能记录"):
        mark_gmail_history_expired(_state("active"), "GMAIL_AUTH_REVOKED")


def test_history_expiry_requires_active() -> None:
    """非 active 连接不能标记失效。"""
    with pytest.raises(RuntimeError, match="只有 active"):
        mark_gmail_history_expired(_state("disconnected"), "GMAIL_HISTORY_EXPIRED")


def test_completing_a_sync_advances_the_cursor() -> None:
    """完成同步推进游标、记录时间并清除错误码。"""
    completed = complete_gmail_sync(
        _state("needs_resync", last_error_code="GMAIL_HISTORY_EXPIRED"),
        "12345", "2026-10-12T09:00:00.000Z",
    )

    assert completed.status == "active"
    assert completed.history_id == "12345"
    assert completed.last_sync_at == "2026-10-12T09:00:00.000Z"
    assert completed.last_error_code is None


def test_disconnected_connection_cannot_complete_a_sync() -> None:
    """**已断开的连接不能"完成"一次不可能发生的同步。**"""
    with pytest.raises(GmailSyncError) as raised:
        complete_gmail_sync(_state("disconnected"), "1", "2026-10-12T09:00:00.000Z")

    assert raised.value.code == "GMAIL_AUTH_REVOKED"


def test_error_code_catalog_is_a_closed_set() -> None:
    """错误码是闭集，便于客户端穷举分支。"""
    assert is_gmail_sync_error_code("GMAIL_HISTORY_EXPIRED") is True
    assert is_gmail_sync_error_code("SOMETHING_ELSE") is False
    assert len(GMAIL_SYNC_ERROR_CODES) == len(set(GMAIL_SYNC_ERROR_CODES)) == 5
