"""Gmail 同步的计划与推进。

镜像 TypeScript 版 ``domain/gmail/sync-state.ts``。

**两处"必须停下来问用户"的设计**：

1. **首次扫描与重扫需要用户明确选择全部邮件或起始日期**。
   邮箱是独立数据源，不能从已有 CSV 推断邮件扫描范围；全量同样保留单次消息上限。
2. **增量响应被截断时判为需要重扫**（``GMAIL_INCREMENTAL_LIMIT_EXCEEDED``）。
   截断意味着这次的增量窗口**不完整**；照常推进游标会**静默漏掉**中间的消息，
   而那些交易本应进入账本。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Union

# 复用同文件族里的两个规范化函数，避免重复实现。
from .sync_state import normalize_gmail_history_id, normalize_gmail_sync_timestamp

GmailSyncStatus = Literal[
    "pending_initial_scan", "active", "needs_resync", "disconnected", "error"
]

GMAIL_SYNC_ERROR_CODES: tuple[str, ...] = (
    "GMAIL_HISTORY_EXPIRED",
    "GMAIL_INCREMENTAL_LIMIT_EXCEEDED",
    "GMAIL_RESYNC_CONFIRMATION_REQUIRED",
    "GMAIL_AUTH_REVOKED",
    "GMAIL_CONNECTION_ERROR",
)


class GmailSyncError(Exception):
    """Gmail 同步的可预期错误。

    @param code: 客户端可稳定分支处理的错误码。
    @param message: 不含凭据的安全说明。
    @param retryable: 是否值得重试。
    """

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class GmailSyncState:
    """连接的同步状态。"""

    status: GmailSyncStatus
    history_id: str | None
    last_sync_at: str | None
    last_error_code: str | None


@dataclass(frozen=True)
class GmailIncrementalRequest:
    """增量同步请求。"""

    mode: Literal["incremental"]
    start_history_id: str


@dataclass(frozen=True)
class GmailBoundedRescanRequest:
    """有界重扫请求。"""

    mode: Literal["bounded_rescan"]
    # None 仅由用户明确选择全部邮件产生，不能从缺失的确认参数推断全量。
    after_date: str | None


GmailSyncRequest = Union[GmailIncrementalRequest, GmailBoundedRescanRequest]


def plan_gmail_sync(
    state: GmailSyncState, *, confirmed_rescan_after_date: str | None = None,
    scan_all: bool = False,
) -> GmailSyncRequest:
    """决定本次同步的方式。

    @param state: 连接状态。
    @param confirmed_rescan_after_date: 用户确认的重扫起始日期。
    @param scan_all: 用户是否显式选择全部嘉信成交邮件；与日期互斥。
    @returns 增量或有界重扫请求。
    @raises GmailSyncError: 游标不可用、缺少重扫确认、连接已断开或处于错误状态。
    """
    if not isinstance(scan_all, bool) or (scan_all and confirmed_rescan_after_date is not None):
        raise ValueError("Gmail 全量扫描和起始日期必须二选一")
    confirmed_scan = scan_all or confirmed_rescan_after_date is not None
    if state.status == "active" and not confirmed_scan:
        if state.history_id is None:
            raise GmailSyncError(
                "GMAIL_HISTORY_EXPIRED", "Gmail 增量游标不可用，必须确认重扫范围"
            )
        return GmailIncrementalRequest(
            mode="incremental", start_history_id=normalize_gmail_history_id(state.history_id)
        )
    if state.status in ("pending_initial_scan", "needs_resync", "active"):
        if not confirmed_scan:
            # 缺少选择不是默认全量，也不能锚定已有文件的日期。
            raise GmailSyncError(
                "GMAIL_RESYNC_CONFIRMATION_REQUIRED",
                "Gmail 首次扫描或重扫必须选择全部邮件或起始日期",
            )
        return GmailBoundedRescanRequest(
            mode="bounded_rescan", after_date=confirmed_rescan_after_date
        )
    if state.status == "disconnected":
        raise GmailSyncError("GMAIL_AUTH_REVOKED", "Gmail 连接已断开，需要重新授权")
    raise GmailSyncError(
        "GMAIL_CONNECTION_ERROR", "Gmail 连接处于错误状态，必须先处理连接错误"
    )


@dataclass(frozen=True)
class GmailIncrementalFetchObservation:
    """一次增量拉取的观察结果。"""

    observed_history_id: str
    candidate_message_count: int
    truncated: bool


@dataclass(frozen=True)
class GmailFetchReady:
    """增量结果可用。"""

    kind: Literal["ready"]
    completion_history_id: str
    candidate_message_count: int


@dataclass(frozen=True)
class GmailFetchNeedsResync:
    """增量结果不可用，需要重扫。"""

    kind: Literal["needs_resync"]
    state: GmailSyncState
    error_code: str


GmailIncrementalFetchDecision = Union[GmailFetchReady, GmailFetchNeedsResync]


def decide_gmail_incremental_fetch(
    state: GmailSyncState, observation: GmailIncrementalFetchObservation
) -> GmailIncrementalFetchDecision:
    """判断一次增量拉取的结果能否推进游标。

    @param state: 连接状态。
    @param observation: 拉取观察结果。
    @returns 就绪或需要重扫。
    @raises GmailSyncError: 连接不是带有效游标的 active 状态。
    @raises ValueError: 候选消息数不是非负整数。
    """
    if state.status != "active" or state.history_id is None:
        raise GmailSyncError(
            "GMAIL_CONNECTION_ERROR", "只有具有有效游标的 active Gmail 连接可以处理增量结果"
        )
    observed = normalize_gmail_history_id(observation.observed_history_id)
    if (
        not isinstance(observation.candidate_message_count, int)
        or isinstance(observation.candidate_message_count, bool)
        or observation.candidate_message_count < 0
    ):
        raise ValueError("Gmail 增量候选消息数必须是非负安全整数")

    if not observation.truncated:
        return GmailFetchReady(
            kind="ready", completion_history_id=observed,
            candidate_message_count=observation.candidate_message_count,
        )
    # **截断意味着增量窗口不完整。** 照常推进游标会静默漏掉中间的消息，
    # 而那些交易本应进入账本。
    error_code = "GMAIL_INCREMENTAL_LIMIT_EXCEEDED"
    return GmailFetchNeedsResync(
        kind="needs_resync", error_code=error_code,
        state=replace(state, status="needs_resync", last_error_code=error_code),
    )


def mark_gmail_history_expired(
    state: GmailSyncState, error_code: str
) -> GmailSyncState:
    """把游标失效标记为需要重扫。

    @param state: 连接状态。
    @param error_code: 必须是 ``GMAIL_HISTORY_EXPIRED``。
    @returns 更新后的状态。
    @raises RuntimeError: 连接不是 active。
    @raises ValueError: 错误码不符。
    """
    if state.status != "active":
        raise RuntimeError(
            f"只有 active Gmail 连接可以标记 history 失效，当前为 {state.status}"
        )
    if error_code != "GMAIL_HISTORY_EXPIRED":
        # 只允许记录这一个稳定错误码：其它错误码对应不同的处置方式，
        # 用它们表示"游标失效"会让调用方做出错误的分支。
        raise ValueError("Gmail history 失效只能记录稳定错误码 GMAIL_HISTORY_EXPIRED")
    return replace(state, status="needs_resync", last_error_code=error_code)


def complete_gmail_sync(
    state: GmailSyncState, new_history_id: str, synced_at: str
) -> GmailSyncState:
    """同步完成：推进游标并清除错误码。

    @raises GmailSyncError: 连接已断开——不能"完成"一次不可能发生的同步。
    """
    if state.status == "disconnected":
        raise GmailSyncError(
            "GMAIL_AUTH_REVOKED", "已断开的 Gmail 连接不能完成同步，需要重新授权"
        )
    return GmailSyncState(
        status="active",
        history_id=normalize_gmail_history_id(new_history_id),
        last_sync_at=normalize_gmail_sync_timestamp(synced_at),
        last_error_code=None,
    )


def is_gmail_sync_error_code(value: str) -> bool:
    """判断是否是已知的同步错误码。"""
    return value in GMAIL_SYNC_ERROR_CODES
