"""Gmail 同步编排。

镜像 TypeScript 版 ``application/gmail-sync-service.ts`` 的 ``sync()``。

**同一错误码在两处有不同映射**，这不是笔误：

| 位置 | `GMAIL_CONNECTION_ERROR` → |
| --- | --- |
| **令牌刷新阶段** | 503 可重试（"Google 服务暂时不可用"） |
| **消息处理阶段**（§119） | 409 不可重试 |

刷新阶段失败时什么业务逻辑都还没跑，重试是安全的；处理阶段失败时可能已经写了一半
账本，盲目重试的语义更复杂。本模块照实镜像两处。

**"需要重扫"是返回值而不是异常**：增量被截断、有界扫描被截断都属于"这次做完了，
但你需要确认重扫范围"——用异常表达会让客户端把它当成故障，而实际上它需要的是
**用户的一个决定**。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..domain.gmail.sync_plan import (
    GmailSyncError,
    decide_gmail_incremental_fetch,
    mark_gmail_history_expired,
    plan_gmail_sync,
)
from .app_error import ApplicationError
from .gmail_sync_errors import completion_state, map_sync_error


@dataclass(frozen=True)
class GmailSyncDependencies:
    """同步流程所需的全部外部动作。

    以回调集合注入，使完整流程可以在没有数据库、网络与时钟的情况下端到端测试。
    """

    load_connection: Callable[[str], Any]
    save_connection: Callable[..., None]
    # (connection, connection_row) -> access token
    refresh_access_token: Callable[[Any], str]
    # (access_token, start_history_id, maximum) -> 增量结果
    fetch_incremental: Callable[..., Any]
    # (access_token, after_date, maximum) -> 有界扫描结果
    fetch_initial: Callable[..., Any]
    process_messages: Callable[..., dict]
    now: Callable[[], str]
    maximum_messages: int


def sync_gmail_connection(
    *,
    connection_id: str,
    confirmed_rescan_after_date: str | None,
    explicit_account_id: str | None,
    dependencies: GmailSyncDependencies,
) -> dict[str, Any]:
    """执行一次 Gmail 同步。

    @param connection_id: 连接 ID。
    @param confirmed_rescan_after_date: 用户确认的重扫起始日期。
    @param explicit_account_id: 用户显式指定的账户。
    @param dependencies: 外部动作集合。
    @returns 同步摘要。
    @raises ApplicationError: 连接不存在（404）、未配置 OAuth（503）、
        需要确认重扫（409）、授权失效（401）、游标失效（409）等。
    """
    connection = dependencies.load_connection(connection_id)
    if connection is None:
        raise ApplicationError(
            code="GMAIL_CONNECTION_ERROR", message="Gmail 连接不存在",
            status_code=404, retryable=False,
        )

    state = _state_of(connection)
    try:
        request = plan_gmail_sync(
            state, confirmed_rescan_after_date=confirmed_rescan_after_date
        )
    except GmailSyncError as error:
        raise map_sync_error(error) from error

    access_token = _refresh_token(connection, dependencies)

    if request.mode == "incremental":
        return _run_incremental(
            connection=connection, state=state, request=request,
            access_token=access_token, explicit_account_id=explicit_account_id,
            dependencies=dependencies,
        )
    return _run_bounded_rescan(
        connection=connection, request=request, access_token=access_token,
        explicit_account_id=explicit_account_id, dependencies=dependencies,
    )


def _refresh_token(connection: Any, dependencies: GmailSyncDependencies) -> str:
    """刷新访问令牌，并把失败落到连接状态上。

    @raises ApplicationError: 授权失效（401）或服务不可用（503，可重试）。
    """
    try:
        return dependencies.refresh_access_token(connection)
    except GmailSyncError as error:
        if error.code == "GMAIL_AUTH_REVOKED":
            # 把授权失效写进连接状态：否则每次同步都会重新试一遍注定失败的刷新。
            _save(connection, dependencies, status="error",
                  last_error_code="GMAIL_AUTH_REVOKED")
            raise ApplicationError(
                code="GMAIL_AUTH_REVOKED", message="Gmail 授权已失效，需要重新授权",
                status_code=401, retryable=False,
            ) from error
        # 刷新阶段的失败一律 503 可重试：此时还没有任何业务逻辑被执行过。
        raise ApplicationError(
            code="GMAIL_CONNECTION_ERROR", message="Google 服务暂时不可用，请稍后重试",
            status_code=503, retryable=True,
        ) from error


def _run_incremental(
    *, connection: Any, state: Any, request: Any, access_token: str,
    explicit_account_id: str | None, dependencies: GmailSyncDependencies,
) -> dict[str, Any]:
    """增量路径。"""
    try:
        fetched = dependencies.fetch_incremental(
            access_token, request.start_history_id, dependencies.maximum_messages
        )
    except GmailSyncError as error:
        if error.code == "GMAIL_HISTORY_EXPIRED":
            expired = mark_gmail_history_expired(state, "GMAIL_HISTORY_EXPIRED")
            _save(connection, dependencies, status=expired.status,
                  last_error_code=expired.last_error_code)
            raise ApplicationError(
                code="GMAIL_HISTORY_EXPIRED",
                message="Gmail 增量游标已失效，必须确认重扫范围",
                status_code=409, retryable=False,
            ) from error
        raise map_sync_error(error) from error

    decision = decide_gmail_incremental_fetch(
        state,
        _observation(
            observed_history_id=fetched.observed_history_id,
            candidate_message_count=len(fetched.records),
            truncated=fetched.truncated,
        ),
    )
    if decision.kind == "needs_resync":
        _save(connection, dependencies, status=decision.state.status,
              last_error_code=decision.state.last_error_code)
        # **返回而不是抛错**：需要的是用户的一个决定，不是一次重试。
        #
        # 状态必须取自决策结果（needs_resync），而不是默认的 active——
        # 报 active 会让客户端以为同步正常结束，于是不会提示用户去确认重扫。
        return _summary(
            mode="incremental", truncated=True, connection=connection,
            history_id=connection.history_id,
            ignored=fetched.ignored_message_count,
            status=decision.state.status,
        )

    summary = dependencies.process_messages(
        connection, fetched.records, explicit_account_id
    )
    _complete(connection, decision.completion_history_id, dependencies)
    return _summary(
        mode="incremental", truncated=False, connection=connection,
        history_id=decision.completion_history_id, ignored=fetched.ignored_message_count,
        summary=summary,
    )


def _run_bounded_rescan(
    *, connection: Any, request: Any, access_token: str,
    explicit_account_id: str | None, dependencies: GmailSyncDependencies,
) -> dict[str, Any]:
    """有界重扫路径。"""
    fetched = dependencies.fetch_initial(
        access_token, request.after_date, dependencies.maximum_messages
    )
    if fetched.kind == "truncated":
        _save(connection, dependencies, status="needs_resync",
              last_error_code="GMAIL_RESYNC_CONFIRMATION_REQUIRED")
        return _summary(
            mode="bounded_rescan", truncated=True, connection=connection,
            history_id=connection.history_id, status="needs_resync", ignored=0,
        )

    summary = dependencies.process_messages(
        connection, fetched.records, explicit_account_id
    )
    _complete(connection, fetched.completion_history_id, dependencies)
    return _summary(
        mode="bounded_rescan", truncated=False, connection=connection,
        history_id=fetched.completion_history_id, ignored=0, summary=summary,
    )


def _complete(
    connection: Any, completion_history_id: str, dependencies: GmailSyncDependencies
) -> None:
    """推进游标并保存。"""
    status, history_id, last_sync_at = completion_state(
        completion_history_id, dependencies.now()
    )
    _save(connection, dependencies, status=status, history_id=history_id,
          last_error_code=None, last_sync_at=last_sync_at)


def _save(
    connection: Any, dependencies: GmailSyncDependencies, **overrides: Any
) -> None:
    """保存连接状态，保留未覆盖的字段与 CAS 基准。"""
    dependencies.save_connection(
        connection, expected_updated_at=connection.updated_at, **overrides
    )


def _summary(
    *, mode: str, truncated: bool, connection: Any, history_id: str | None,
    ignored: int, summary: dict | None = None, status: str = "active",
) -> dict[str, Any]:
    """构造同步响应。

    连接字段里的游标是**调用方本次实际拥有的那个**：被截断时它是旧值，
    因为本次并没有推进它。
    """
    counts = summary or {
        "importedMessages": 0, "alreadyImportedMessages": 0,
        "conflictMessages": 0, "rejectedMessages": 0,
    }
    return {
        "mode": mode,
        "truncated": truncated,
        **counts,
        "ignoredMessages": ignored,
        "connection": {
            "id": connection.id,
            "status": status,
            # 被截断时状态可能是 needs_resync，但游标仍是旧值。
            "historyId": history_id,
            "lastSyncAt": connection.last_sync_at,
        },
    }


def _state_of(connection: Any) -> Any:
    """把连接行转换为同步状态。"""
    from ..domain.gmail.sync_plan import GmailSyncState

    return GmailSyncState(
        status=connection.status, history_id=connection.history_id,
        last_sync_at=connection.last_sync_at,
        last_error_code=connection.last_error_code,
    )


def _observation(**kwargs: Any) -> Any:
    """构造增量观察结果。"""
    from ..domain.gmail.sync_plan import GmailIncrementalFetchObservation

    return GmailIncrementalFetchObservation(**kwargs)
