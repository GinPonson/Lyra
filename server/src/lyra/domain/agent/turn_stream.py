"""Agent 回合的流式状态机。

镜像 TypeScript 版 ``domain/agent/turn-stream.ts``。

四条性质，每条都对应一种真实故障：

1. **模型隐藏推理绝不进入状态或传输层。** `reasoning.delta` 只发出
   ``{"state": "active"}``，原文既不写入状态、也不进入重连缓冲——客户端只需要驱动
   "思考中"动效。**把推理存进状态，它就会随重连缓冲回放给用户。**
2. **`message_snapshot` 覆盖增量聚合结果**，因为那是供应商的权威可见文本；继续
   在它之上追加增量会产生重复文本。
3. **`settled` 之后拒绝任何新信号**，防止把两个回合的文本拼在一起。
4. **失败后不接受成功快照，成功快照后不接受失败**——两个方向都要挡。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Union

from .events import AgentEvent, AgentEventSequencer
from .switching import AgentSessionSnapshot
from .turn import (
    AgentTurnCompletionPlan,
    AgentTurnFailureOutcome,
    AgentTurnFailurePlan,
    complete_agent_turn,
    fail_agent_turn,
)


@dataclass(frozen=True)
class MessageDelta:
    """可见文本增量。"""

    delta: str


@dataclass(frozen=True)
class ReasoningDelta:
    """模型隐藏推理增量；原文不进入状态。"""

    delta: str


@dataclass(frozen=True)
class MessageSnapshot:
    """供应商的权威可见文本快照。"""

    content: str
    stop_reason: str


@dataclass(frozen=True)
class UsageUpdated:
    """用量更新。"""

    total_tokens: int
    total_cost: float | None


@dataclass(frozen=True)
class MessageFailed:
    """供应商上报的失败终态。"""

    outcome: AgentTurnFailureOutcome


@dataclass(frozen=True)
class TurnSettled:
    """回合结束信号。"""


AgentTurnStreamSignal = Union[
    MessageDelta, ReasoningDelta, MessageSnapshot, UsageUpdated, MessageFailed, TurnSettled
]


@dataclass(frozen=True)
class AgentTurnStreamState:
    """单个逻辑回合的可恢复流状态；**不保存 reasoning 或供应商原始帧**。"""

    session_id: str
    message_id: str
    visible_content: str
    # 收到 message_snapshot 后为真，此时 visible_content 是供应商权威快照。
    has_authoritative_snapshot: bool
    failure_outcome: AgentTurnFailureOutcome | None
    settled: bool
    total_tokens: int | None
    total_cost: float | None
    last_event_sequence: int


@dataclass(frozen=True)
class AgentTurnStreamUpdate:
    """应用一个信号后的新状态和可立即推送的事件。"""

    state: AgentTurnStreamState
    event: AgentEvent | None


def create_agent_turn_stream_state(
    *, session: AgentSessionSnapshot, message_id: str, last_event_sequence: int
) -> AgentTurnStreamState:
    """建立绑定会话、消息与已持久化序号的全新流状态。

    @param session: 必须处于 ``running``。
    @param message_id: 本回合的助手消息 ID。
    @param last_event_sequence: 已持久化的事件序号。
    @returns 空流状态。
    @raises ValueError: 会话不在 running，或参数非法。
    """
    if session.status != "running":
        raise ValueError("只有 running 会话可以建立 Agent 消息流")
    resolved_id = _require_text(message_id, "Agent 流消息 ID")
    _require_sequence(last_event_sequence, "Agent 流事件序号")
    return AgentTurnStreamState(
        session_id=session.id,
        message_id=resolved_id,
        visible_content="",
        has_authoritative_snapshot=False,
        failure_outcome=None,
        settled=False,
        total_tokens=None,
        total_cost=None,
        last_event_sequence=last_event_sequence,
    )


def apply_agent_turn_stream_signal(
    *,
    state: AgentTurnStreamState,
    session: AgentSessionSnapshot,
    signal: AgentTurnStreamSignal,
    occurred_at: str,
) -> AgentTurnStreamUpdate:
    """累积一个供应商信号并按需生成统一实时事件。

    @param state: 当前流状态。
    @param session: 所属会话（用于事件里的供应商与账本版本）。
    @param signal: 供应商信号。
    @param occurred_at: 事件时间。
    @returns 新状态与可推送事件（无需推送时为 ``None``）。
    @raises ValueError: 状态与会话不匹配、已 settled，或信号与已有终态冲突。
    """
    if state.session_id != session.id:
        raise ValueError("Agent 流状态与会话不匹配")
    if state.settled:
        # 允许继续追加会把两个回合的文本拼在一起。
        raise ValueError("Agent 流已经 settled，不能继续追加信号")

    resolved_at = _require_text(occurred_at, "Agent 流事件时间")
    current = state
    event_type: str | None = None
    payload: dict[str, object] = {}

    if isinstance(signal, MessageDelta):
        _reject_after_terminal(current, "文本")
        if signal.delta == "":
            return AgentTurnStreamUpdate(state=current, event=None)
        current = replace(current, visible_content=current.visible_content + signal.delta)
        # 事件字段名必须与客户端读取的键一致（`text`）；两端改名会让增量被静默丢弃，
        # 客户端只在终态快照时一次性看到全文。
        event_type, payload = "message.delta", {"text": signal.delta}

    elif isinstance(signal, ReasoningDelta):
        _reject_after_terminal(current, "reasoning")
        if signal.delta == "":
            return AgentTurnStreamUpdate(state=current, event=None)
        # **原文既不写入状态、也不进入事件**——只发一个"思考中"标记。
        event_type, payload = "reasoning.delta", {"state": "active"}

    elif isinstance(signal, MessageSnapshot):
        if current.failure_outcome is not None:
            raise ValueError("Agent 失败后不能接受成功消息快照")
        current = replace(
            current, visible_content=signal.content, has_authoritative_snapshot=True
        )

    elif isinstance(signal, UsageUpdated):
        _require_non_negative(signal.total_tokens, "Agent total tokens")
        if signal.total_cost is not None:
            _require_non_negative(signal.total_cost, "Agent total cost")
        current = replace(
            current, total_tokens=signal.total_tokens, total_cost=signal.total_cost
        )
        event_type = "usage.updated"
        payload = {"total_tokens": signal.total_tokens, "total_cost": signal.total_cost}

    elif isinstance(signal, MessageFailed):
        if current.has_authoritative_snapshot:
            raise ValueError("Agent 成功消息快照后不能改写为失败")
        if current.failure_outcome is not None and current.failure_outcome != signal.outcome:
            raise ValueError("Agent 流收到冲突的失败终态")
        current = replace(current, failure_outcome=signal.outcome)

    else:  # TurnSettled
        current = replace(current, settled=True)

    if event_type is None:
        return AgentTurnStreamUpdate(state=current, event=None)

    event = AgentEventSequencer(current.session_id, current.last_event_sequence).next(
        event_type=event_type,  # type: ignore[arg-type]
        message_id=current.message_id,
        occurred_at=resolved_at,
        payload={
            **payload,
            "provider": session.provider,
            "ledger_version": session.ledger_version,
        },
    )
    return AgentTurnStreamUpdate(
        state=replace(current, last_event_sequence=event.sequence), event=event
    )


def finalize_agent_turn_stream(
    *,
    state: AgentTurnStreamState,
    session: AgentSessionSnapshot,
    finished_at: str,
) -> AgentTurnCompletionPlan | AgentTurnFailurePlan:
    """settled 后复用既有原子计划生成唯一持久终态。

    **成功必须有非空权威快照**；否则按明确失败收口——不能把"没有拿到任何权威文本"
    当成一次成功但空的回答。

    取消必须由调用方先把会话状态转为 ``cancelling``，避免从供应商退出码猜测用户意图。

    @param state: 已 settled 的流状态。
    @param session: 当前会话。
    @param finished_at: 结束时间。
    @returns 完成或失败计划。
    @raises ValueError: 状态与会话不匹配，或流尚未 settled。
    """
    if state.session_id != session.id:
        raise ValueError("Agent 流状态与会话不匹配")
    if not state.settled:
        raise ValueError("Agent 流尚未 settled，不能持久化终态")

    if state.failure_outcome is not None:
        return fail_agent_turn(
            session=session, message_id=state.message_id, outcome=state.failure_outcome,
            partial_content=state.visible_content, finished_at=finished_at,
            last_event_sequence=state.last_event_sequence,
        )
    if not state.has_authoritative_snapshot or state.visible_content.strip() == "":
        # 没有权威文本就不算成功，即使供应商没有报错。
        return fail_agent_turn(
            session=session, message_id=state.message_id, outcome="failed",
            partial_content=state.visible_content, finished_at=finished_at,
            last_event_sequence=state.last_event_sequence,
        )
    return complete_agent_turn(
        session=session, message_id=state.message_id, content=state.visible_content,
        completed_at=finished_at, last_event_sequence=state.last_event_sequence,
    )


def _reject_after_terminal(state: AgentTurnStreamState, label: str) -> None:
    """终态快照或失败后不得追加增量。

    @raises ValueError: 已有权威快照或失败终态。
    """
    if state.failure_outcome is not None or state.has_authoritative_snapshot:
        raise ValueError(f"Agent 终态快照或失败后不能追加{label}增量")


def _require_text(value: str, label: str) -> str:
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{label}不能为空")
    return normalized


def _require_sequence(value: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label}必须是非负安全整数")
    return value


def _require_non_negative(value: float, label: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{label}必须是非负有限数值")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label}必须是非负有限数值")
