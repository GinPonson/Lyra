"""Agent 流式事件与重连续传。

镜像 TypeScript 版 ``domain/agent/events.ts``。

**核心原则：缺口绝不补造。** 客户端重连时若事件序列有缺口，只能让它重新取一份
规范会话快照，**不能猜、不能拼接、不能补造 delta**。

理由：事件是增量的（`message.delta` 只带新增文本）。若服务端凭空补一条"看起来
合理"的 delta，客户端会把不完整的文本当成最终答案——**而没有任何一方能察觉**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

AGENT_EVENT_TYPES: tuple[str, ...] = (
    "session.started",
    "message.started",
    "message.delta",
    "reasoning.delta",
    "tool.started",
    "tool.completed",
    "approval.required",
    "usage.updated",
    "message.completed",
    "message.failed",
    "session.completed",
)

AgentEventType = Literal[
    "session.started", "message.started", "message.delta", "reasoning.delta",
    "tool.started", "tool.completed", "approval.required", "usage.updated",
    "message.completed", "message.failed", "session.completed",
]

ResumeMode = Literal["events", "snapshot_required"]
SnapshotReason = Literal["client_ahead", "event_gap", "mixed_session"]


@dataclass(frozen=True)
class AgentEvent:
    """发送给 Flutter 的统一事件信封。"""

    type: AgentEventType
    session_id: str
    message_id: str | None
    # 在单个会话内严格递增，从 1 开始。
    sequence: int
    occurred_at: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class AgentEventResume:
    """续传结果。"""

    mode: ResumeMode
    events: tuple[AgentEvent, ...] = ()
    reason: SnapshotReason | None = None


class AgentEventSequencer:
    """为单个会话分配单调递增的事件序号。

    不负责发送或持久化——那是传输层与仓储层的事，领域层只保证序号本身正确。

    @param session_id: 会话 ID。
    @param last_sequence: 已发出或已持久化的最后序号；新会话为 0。
    """

    def __init__(self, session_id: str, last_sequence: int = 0) -> None:
        if session_id.strip() == "":
            raise ValueError("sessionId 不能为空")
        if (
            not isinstance(last_sequence, int)
            or isinstance(last_sequence, bool)
            or last_sequence < 0
        ):
            raise ValueError("lastSequence 必须是非负安全整数")
        self._session_id = session_id
        self._sequence = last_sequence

    @property
    def sequence(self) -> int:
        """当前序号。"""
        return self._sequence

    def next(
        self,
        *,
        event_type: AgentEventType,
        message_id: str | None,
        occurred_at: str,
        payload: dict[str, Any],
    ) -> AgentEvent:
        """生成下一条事件。

        @param event_type: 事件类型。
        @param message_id: 关联消息；无关联时为 ``None``。
        @param occurred_at: 发生时间。
        @param payload: 事件负载。
        @returns 带会话 ID 与下一序号的事件。
        @raises ValueError: 时间戳为空。
        """
        if occurred_at.strip() == "":
            raise ValueError("Agent 事件时间戳不能为空")
        self._sequence += 1
        return AgentEvent(
            type=event_type,
            session_id=self._session_id,
            message_id=message_id,
            sequence=self._sequence,
            occurred_at=occurred_at,
            payload=payload,
        )


def are_agent_events_contiguous(previous: AgentEvent, next_event: AgentEvent) -> bool:
    """判断同一会话的相邻事件是否连续。"""
    return (
        previous.session_id == next_event.session_id
        and next_event.sequence == previous.sequence + 1
    )


def plan_agent_event_resume(
    *,
    session_id: str,
    after_sequence: int,
    last_persisted_sequence: int,
    buffered_events: tuple[AgentEvent, ...],
) -> AgentEventResume:
    """判定重连后能否从缓冲区安全续传。

    只有缓冲区**完整覆盖且严格连续**时才返回增量事件；客户端游标超前、缓冲区有缺口
    或混入其它会话，一律要求重新获取规范快照。

    @param session_id: 当前会话 ID。
    @param after_sequence: 客户端已收到的最后序号。
    @param last_persisted_sequence: 服务端已持久化的最后序号。
    @param buffered_events: 服务端内存缓冲区里的事件。
    @returns 续传计划。
    @raises ValueError: 会话 ID 为空或游标非法。
    """
    resolved = session_id.strip()
    if resolved == "":
        raise ValueError("Agent 事件恢复 sessionId 不能为空")
    _require_sequence(after_sequence, "Agent 客户端事件游标")
    _require_sequence(last_persisted_sequence, "Agent 服务端事件游标")

    if after_sequence > last_persisted_sequence:
        # 客户端比服务端"更靠前"：它拿到过服务端不知道的事件，两边状态已不可信。
        return AgentEventResume(mode="snapshot_required", reason="client_ahead")
    if any(event.session_id != resolved for event in buffered_events):
        return AgentEventResume(mode="snapshot_required", reason="mixed_session")
    if after_sequence == last_persisted_sequence:
        return AgentEventResume(mode="events", events=())

    events = tuple(
        event
        for event in buffered_events
        if after_sequence < event.sequence <= last_persisted_sequence
    )
    expected = after_sequence + 1
    for event in events:
        if event.sequence != expected:
            return AgentEventResume(mode="snapshot_required", reason="event_gap")
        expected += 1
    if expected != last_persisted_sequence + 1:
        # 缓冲区不足以覆盖到已持久化的末尾——缺的正是最后一段。
        return AgentEventResume(mode="snapshot_required", reason="event_gap")
    return AgentEventResume(mode="events", events=events)


def _require_sequence(value: object, label: str) -> int:
    """要求非负整数游标。"""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label}必须是非负安全整数")
    return value
