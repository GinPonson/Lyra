"""Gmail 同步错误到应用错误的映射。

镜像 TypeScript 版 ``gmail-sync-service.ts`` 的 ``#mapSyncError``。

| 同步错误码 | HTTP | 可重试 |
| --- | --- | --- |
| ``GMAIL_RESYNC_CONFIRMATION_REQUIRED`` | 409 | 否 |
| ``GMAIL_AUTH_REVOKED`` | **401** | 否 |
| ``GMAIL_HISTORY_EXPIRED`` | 409 | 否 |
| 其它同步错误（含 ``GMAIL_CONNECTION_ERROR``） | 409 | 否 |
| 已是应用错误 | 透传 | 透传 |
| 未知异常 | 503 | **是** |

一处值得记录的行为：``GMAIL_CONNECTION_ERROR`` 在同步层是**可重试**的（网络抖动、
5xx），但到了这一层被硬编码成 **409 不可重试**。这会让客户端对一次真正暂时的故障
不再重试。

**这是参考实现的既有行为，本模块照实镜像。** 迁移的目标是行为一致；在这里"顺手修好"
会让两侧在同一类故障上给出不同的提示与重试策略，而差异会在切流当天才暴露。
"""

from __future__ import annotations

from typing import Any

from ..domain.gmail.sync_plan import GmailSyncError
from .app_error import ApplicationError

# 同步错误码 → (HTTP 状态, 是否可重试, 面向用户的说明)
_SYNC_ERROR_MAP: dict[str, tuple[int, bool, str]] = {
    "GMAIL_RESYNC_CONFIRMATION_REQUIRED": (
        409, False, "Gmail 首次扫描或重扫必须确认起始日期",
    ),
    "GMAIL_AUTH_REVOKED": (401, False, "Gmail 连接已断开，需要重新授权"),
    "GMAIL_HISTORY_EXPIRED": (409, False, "Gmail 增量游标不可用，必须确认重扫范围"),
}


def map_sync_error(error: Exception) -> ApplicationError:
    """把同步异常映射为带 HTTP 语义的应用错误。

    @param error: 任意异常。
    @returns 应用错误。
    """
    if isinstance(error, GmailSyncError):
        known = _SYNC_ERROR_MAP.get(error.code)
        if known is not None:
            status_code, retryable, message = known
            return ApplicationError(
                code=error.code, message=message,
                status_code=status_code, retryable=retryable,
            )
        # 其余同步错误（含 GMAIL_CONNECTION_ERROR / GMAIL_INCREMENTAL_LIMIT_EXCEEDED）
        # 统一为 409 不可重试。
        return ApplicationError(
            code="GMAIL_CONNECTION_ERROR",
            message="Gmail 连接处于错误状态",
            status_code=409,
            retryable=False,
        )
    if isinstance(error, ApplicationError):
        # 已经是应用错误：原样透传，避免丢失更精确的语义。
        return error
    return ApplicationError(
        code="GMAIL_CONNECTION_ERROR",
        message="Gmail 同步失败",
        status_code=503,
        retryable=True,
    )


def completion_state(
    completion_history_id: str, synced_at: str
) -> tuple[str, str, str | None]:
    """同步成功后的连接状态字段。

    **刻意传一个合成的 ``active`` 状态给 ``complete_gmail_sync``**，而不是连接当前的
    状态——与参考实现一致。

    这意味着 ``complete_gmail_sync`` 的"已断开不能完成同步"守卫在这一条路径上
    **不会触发**：走到这里说明连接既不是断开也不是错误（否则早就返回了）。
    照实镜像这一顺序，避免两侧在同一状态下给出不同结果。

    @param completion_history_id: 本次读到的游标。
    @param synced_at: 服务端可信时间。
    @returns ``(status, history_id, last_sync_at)``。
    """
    from ..domain.gmail.sync_plan import GmailSyncState, complete_gmail_sync

    synthetic = GmailSyncState(
        status="active", history_id=None, last_sync_at=None, last_error_code=None
    )
    completed = complete_gmail_sync(synthetic, completion_history_id, synced_at)
    return completed.status, completed.history_id or "", completed.last_sync_at


def refresh_with_connection(
    connection: object, *, cipher: object, post_form: Any, now: Any
) -> str:
    """解密连接的 refresh token 并刷新 access token。

    把"解密 → 刷新"两步合成一个入口，避免调用方各自拼装时漏掉解密环节的作用域校验。

    @param connection: 连接行。
    @param cipher: 凭据加密器。
    @param post_form: 令牌端点传输层。
    @param now: 返回服务端可信时间的可调用对象。
    @returns 新的 access token。
    @raises GmailSyncError: 凭据缺失/无法解密（连接错误）或授权失效（需重新授权）。
    """
    from ..infrastructure.gmail.google_oauth_client import (
        decrypt_connection_refresh_token,
        refresh_google_access_token,
    )
    from ..infrastructure.gmail.oauth_transaction import GmailOAuthError

    refresh_token = decrypt_connection_refresh_token(connection, cipher)
    oauth = _oauth_config()
    if oauth is None:
        raise GmailSyncError(
            "GMAIL_CONNECTION_ERROR", "Google OAuth 客户端尚未配置"
        )
    try:
        refreshed = refresh_google_access_token(
            refresh_token=refresh_token, client_id=oauth["client_id"],
            granted_scope_text=connection.granted_scopes,
            client_secret=oauth.get("client_secret"),
            now=now(), post_form=post_form,
        )
    except GmailOAuthError as error:
        # 作用域不符属于配置问题，不是暂时性故障。
        raise GmailSyncError("GMAIL_CONNECTION_ERROR", error.message) from error
    return refreshed["accessToken"]


def _oauth_config() -> dict | None:
    """从进程环境读取 OAuth 配置。

    **每次都读环境**：这让测试可以在不重建服务的情况下替换配置，也避免把配置快照
    与它实际生效的时点脱钩。
    """
    import os

    client_id = (os.environ.get("LYRA_GOOGLE_CLIENT_ID") or "").strip()
    if client_id == "":
        return None
    return {
        "client_id": client_id,
        "client_secret": (os.environ.get("LYRA_GOOGLE_CLIENT_SECRET") or "").strip() or None,
    }
