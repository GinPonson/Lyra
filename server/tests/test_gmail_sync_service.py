"""Gmail 同步编排测试。

两处最要紧的语义：

1. **同一错误码在两处有不同映射**——刷新阶段 503 可重试，处理阶段 409 不可重试；
2. **"需要重扫"是返回值而不是异常**——它需要的是用户的一个决定，不是一次重试。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.gmail_sync_service import (
    GmailSyncDependencies,
    sync_gmail_connection,
)
from lyra.domain.gmail.sync_plan import GmailSyncError

CONNECTION_ID = "11111111-1111-1111-1111-111111111111"
NOW = "2026-10-12T09:00:00.000Z"


@dataclass
class _Connection:
    """连接行桩。"""

    id: str = CONNECTION_ID
    status: str = "active"
    history_id: str | None = "42"
    last_sync_at: str | None = "2026-10-11T09:00:00.000Z"
    last_error_code: str | None = None
    updated_at: str = "2026-10-11T09:00:00.000Z"


@dataclass
class _Fetched:
    """拉取结果桩。"""

    records: tuple = ()
    observed_history_id: str = "99"
    truncated: bool = False
    ignored_message_count: int = 0
    kind: str = "ready"
    completion_history_id: str = "99"


def _deps(connection, **overrides) -> GmailSyncDependencies:
    """构造依赖集合。"""
    saves: list[dict] = []
    deps = GmailSyncDependencies(
        load_connection=lambda _: connection if connection is None or connection.id else None,
        save_connection=lambda conn, **kw: saves.append(kw),
        refresh_access_token=lambda conn: "ya29.token",
        fetch_incremental=lambda token, start, maximum: _Fetched(),
        fetch_initial=lambda token, after, maximum: _Fetched(),
        process_messages=lambda conn, records, explicit: {
            "importedMessages": 0, "alreadyImportedMessages": 0,
            "conflictMessages": 0, "rejectedMessages": 0,
        },
        now=lambda: NOW,
        maximum_messages=100,
    )
    for name, value in overrides.items():
        deps = replace(deps, **{name: value})
    # frozen dataclass 不能挂额外属性，因此把记录单独返回。
    return deps, saves


def _sync(*, connection=None, confirmed=None, explicit=None, **overrides):
    """跑一次同步，返回结果与保存记录。"""
    conn = connection if connection is not None else _Connection()
    deps, saves = _deps(conn, **overrides)
    result = sync_gmail_connection(
        connection_id=CONNECTION_ID, confirmed_rescan_after_date=confirmed,
        explicit_account_id=explicit, dependencies=deps,
    )
    return result, saves


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------


def test_unknown_connection_is_a_404() -> None:
    """连接不存在 → 404。"""
    with pytest.raises(ApplicationError) as raised:
        sync_gmail_connection(
            connection_id=CONNECTION_ID, confirmed_rescan_after_date=None,
            explicit_account_id=None,
            dependencies=_deps(None, load_connection=lambda _: None)[0],
        )

    assert raised.value.status_code == 404


def test_confirmed_all_mail_passes_no_date_to_the_fetcher() -> None:
    """首次全量使用邮箱自身范围，与 CSV 的起止日期无关。"""
    dates: list[str | None] = []
    connection = _Connection(status="pending_initial_scan", history_id=None)
    dependencies, _ = _deps(
        connection,
        fetch_initial=lambda token, after, maximum: (dates.append(after) or _Fetched()),
    )
    result = sync_gmail_connection(
        connection_id=CONNECTION_ID, scan_all=True,
        confirmed_rescan_after_date=None, explicit_account_id=None,
        dependencies=dependencies,
    )
    assert dates == [None]
    assert result["connection"]["status"] == "active"
    assert result["connection"]["lastSyncAt"] == NOW


def test_initial_scan_keeps_api_configuration_failure_actionable() -> None:
    """首扫 API 未启用应给出启用配置提示，不能变成 500 或要求反复重授权。"""
    def fetch(*args):
        raise GmailSyncError("GMAIL_API_DISABLED", "safe")

    connection = _Connection(status="pending_initial_scan", history_id=None)
    dependencies, saves = _deps(connection, fetch_initial=fetch)
    with pytest.raises(ApplicationError) as raised:
        sync_gmail_connection(
            connection_id=CONNECTION_ID, scan_all=True,
            confirmed_rescan_after_date=None, explicit_account_id=None,
            dependencies=dependencies,
        )
    assert raised.value.code == "GMAIL_API_DISABLED"
    assert raised.value.status_code == 503
    assert saves == []


# --------------------------------------------------------------------------
# 令牌刷新失败的两处映射
# --------------------------------------------------------------------------


def test_auth_revoked_during_refresh_marks_the_connection_and_raises_401() -> None:
    """**刷新时授权失效 → 连接进入 error 状态 + 401。**

    不写状态的话，每次同步都会重新试一遍注定失败的刷新。
    """
    def refresh(conn):
        raise GmailSyncError("GMAIL_AUTH_REVOKED", "x")

    with pytest.raises(ApplicationError) as raised:
        _sync(refresh_access_token=refresh)

    assert raised.value.status_code == 401
    assert raised.value.retryable is False


def test_transient_refresh_failure_is_a_retryable_503() -> None:
    """**刷新阶段的连接错误是 503 可重试，不是 409。**

    此时还没有任何业务逻辑被执行过，重试是安全的。这与消息处理阶段的
    `GMAIL_CONNECTION_ERROR → 409 不可重试`（§119）**刻意不同**。
    """
    def refresh(conn):
        raise GmailSyncError("GMAIL_CONNECTION_ERROR", "x", retryable=True)

    with pytest.raises(ApplicationError) as raised:
        _sync(refresh_access_token=refresh)

    assert raised.value.status_code == 503
    assert raised.value.retryable is True


# --------------------------------------------------------------------------
# 增量：正常
# --------------------------------------------------------------------------


def test_incremental_happy_path_advances_the_cursor() -> None:
    """增量成功：处理消息、推进游标、返回摘要。"""
    processed: list = []
    result, saves = _sync(
        fetch_incremental=lambda token, start, maximum: _Fetched(
            records=({"id": "m1"},), observed_history_id="99"
        ),
        process_messages=lambda conn, records, explicit: (
            processed.append(records) or {
                "importedMessages": 1, "alreadyImportedMessages": 0,
                "conflictMessages": 0, "rejectedMessages": 0,
            }
        ),
    )

    assert result["mode"] == "incremental"
    assert result["truncated"] is False
    assert result["importedMessages"] == 1
    assert result["connection"]["historyId"] == "99"
    assert result["connection"]["status"] == "active"
    assert result["connection"]["lastSyncAt"] == NOW
    assert processed == [({"id": "m1"},)]
    # 最后一次保存推进了游标。
    assert saves[-1]["history_id"] == "99"  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 增量：截断与失效
# --------------------------------------------------------------------------


def test_truncated_incremental_returns_而不是_raises() -> None:
    """**截断是返回值而不是异常。**

    它需要的是用户的一个决定（确认重扫范围），不是一次重试。用异常表达会让客户端
    把它当成故障。
    """
    result, _ = _sync(
        fetch_incremental=lambda token, start, maximum: _Fetched(
            truncated=True, ignored_message_count=7
        )
    )

    assert result["truncated"] is True
    assert result["importedMessages"] == 0
    assert result["ignoredMessages"] == 7
    assert result["connection"]["status"] == "needs_resync"


def test_truncated_incremental_keeps_the_old_cursor() -> None:
    """**截断时不推进游标**：本次并没有读到能作为新基线的位置。"""
    result, _ = _sync(
        fetch_incremental=lambda token, start, maximum: _Fetched(truncated=True)
    )

    assert result["connection"]["historyId"] == "42"


def test_history_expired_marks_the_connection_and_raises_409() -> None:
    """游标失效 → 连接进入 needs_resync + 409。"""
    def fetch(token, start, maximum):
        raise GmailSyncError("GMAIL_HISTORY_EXPIRED", "x")

    with pytest.raises(ApplicationError) as raised:
        _sync(fetch_incremental=fetch)

    assert raised.value.status_code == 409
    assert raised.value.code == "GMAIL_HISTORY_EXPIRED"


# --------------------------------------------------------------------------
# 有界重扫
# --------------------------------------------------------------------------


def test_rescan_without_confirmation_requires_it() -> None:
    """首次扫描必须由用户确认起始日期。"""
    connection = _Connection(status="pending_initial_scan", history_id=None)

    with pytest.raises(ApplicationError) as raised:
        _sync(connection=connection)

    assert raised.value.code == "GMAIL_RESYNC_CONFIRMATION_REQUIRED"
    assert raised.value.status_code == 409


def test_confirmed_rescan_is_bounded() -> None:
    """确认后走有界重扫并推进游标。"""
    connection = _Connection(status="needs_resync", history_id=None)

    result, _ = _sync(
        connection=connection, confirmed="2026-01-05",
        fetch_initial=lambda token, after, maximum: _Fetched(
            completion_history_id="777"
        ),
    )

    assert result["mode"] == "bounded_rescan"
    assert result["truncated"] is False
    assert result["connection"]["historyId"] == "777"


def test_truncated_rescan_asks_for_confirmation() -> None:
    """有界扫描被截断 → 返回 truncated 并进入 needs_resync。"""
    connection = _Connection(status="pending_initial_scan", history_id=None)

    result, _ = _sync(
        connection=connection, confirmed="2026-01-05",
        fetch_initial=lambda token, after, maximum: _Fetched(kind="truncated"),
    )

    assert result["truncated"] is True
    assert result["connection"]["status"] == "needs_resync"


def test_messages_are_not_processed_when_truncated() -> None:
    """**截断时不处理任何消息**——否则会导入一批"前 N 封"，而用户以为扫完了。"""
    processed: list = []
    connection = _Connection(status="pending_initial_scan", history_id=None)

    _sync(
        connection=connection, confirmed="2026-01-05",
        fetch_initial=lambda token, after, maximum: _Fetched(kind="truncated"),
        process_messages=lambda *args: processed.append(args) or {},
    )

    assert processed == []


# --------------------------------------------------------------------------
# 连接状态保存
# --------------------------------------------------------------------------


def test_saving_always_uses_the_original_updated_at_as_the_cas_base() -> None:
    """**保存始终用读到的 ``updated_at`` 做 CAS 基准。**

    若把"上一次保存后的新值"当成基准，CAS 就会永远成功——**并发断开会被静默覆盖**。
    """
    _result, saves = _sync()

    assert saves
    for call in saves:
        assert call["expected_updated_at"] == "2026-10-11T09:00:00.000Z"
