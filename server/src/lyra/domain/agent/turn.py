"""Agent 回合的开始、完成与失败计划。

镜像 TypeScript 版 ``domain/agent/turn.ts``。

三个函数各产出一份**可原子持久化的计划**（会话快照 + 消息 + 事件）。分开的理由是
三条路径的语义不同，合并会掩盖差异：

| 路径 | 前提状态 | 消息 |
| --- | --- | --- |
| 开始 | `idle` | 用户消息 |
| 完成 | `running` | 助手消息，**内容必须非空** |
| 失败 / 取消 | `failed`→`running`；`cancelled`→`cancelling` | **只有产生过可见文本才留消息** |

两处刻意的约束：

1. **成功回合的助手内容必须非空**。空内容走失败或取消路径；用"空成功消息"收口
   会让客户端显示一个没有任何内容的回答，且看起来是正常的。
2. **失败/取消时若没有可见文本就不生成消息**。凭空造一条空助手消息会让会话历史
   里出现一段"模型说过话但什么都没说"的记录。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from .events import AgentEvent, AgentEventSequencer
from .lifecycle import AgentSessionStateError, transition_agent_session
from .switching import AgentSessionSnapshot, NormalizedAgentMessage

AgentTurnFailureOutcome = Literal["failed", "cancelled"]

TURN_FAILED_CODE = "AGENT_TURN_FAILED"
TURN_CANCELLED_CODE = "AGENT_TURN_CANCELLED"


@dataclass(frozen=True)
class AgentTurnStartPlan:
    """开始一个回合。"""

    session: AgentSessionSnapshot
    user_message: NormalizedAgentMessage
    event: AgentEvent
    adapter_input: dict[str, str]


@dataclass(frozen=True)
class AgentTurnCompletionPlan:
    """成功收口一个回合。"""

    session: AgentSessionSnapshot
    assistant_message: NormalizedAgentMessage
    event: AgentEvent


@dataclass(frozen=True)
class AgentTurnFailurePlan:
    """失败或取消收口一个回合。"""

    session: AgentSessionSnapshot
    # 只有供应商已经产生用户可见的部分回答时才持久化消息。
    assistant_message: NormalizedAgentMessage | None
    event: AgentEvent


def start_agent_turn(
    *,
    session: AgentSessionSnapshot,
    message_id: str,
    content: str,
    started_at: str,
    last_event_sequence: int,
) -> AgentTurnStartPlan:
    """固化用户消息并生成 ``message.started`` 事件。

    本函数**不启动任何外部进程**。调用方应先在单写事务中保存 running 快照、用户
    消息与事件序号，成功后再把 ``adapter_input`` 交给供应商协议——这样进程启动
    失败也能由后续明确的失败事件收口，而不会出现**未记录的模型回合**。

    @param session: 当前会话。
    @param message_id: 客户端生成的消息 ID。
    @param content: 用户内容。
    @param started_at: 回合开始时间。
    @param last_event_sequence: 已持久化的事件序号。
    @returns 回合起始计划。
    @raises AgentSessionStateError: 会话不在 ``idle``。
    @raises ValueError: 参数为空或消息 ID 已存在。
    """
    if session.status != "idle":
        raise AgentSessionStateError(session.status, "running")
    resolved_id = _require_text(message_id, "Agent 用户消息 ID")
    resolved_content = _require_text(content, "Agent 用户消息")
    _require_new_message(session, resolved_id)
    started = _require_text(started_at, "Agent 回合开始时间")

    user_message = NormalizedAgentMessage(
        id=resolved_id, role="user", content=resolved_content, created_at=started
    )
    running = transition_agent_session(session, "running")
    next_session = replace(running, messages=(*running.messages, user_message))
    event = AgentEventSequencer(session.id, last_event_sequence).next(
        event_type="message.started",
        message_id=resolved_id,
        occurred_at=started,
        payload={
            "role": "user",
            "provider": session.provider,
            "ledger_version": session.ledger_version,
            "account_scope": {
                "kind": session.account_scope.kind,
                **(
                    {"account": session.account_scope.account}
                    if session.account_scope.kind == "account"
                    else {}
                ),
            },
        },
    )
    return AgentTurnStartPlan(
        session=next_session,
        user_message=user_message,
        event=event,
        adapter_input={"messageId": resolved_id, "content": resolved_content},
    )


def complete_agent_turn(
    *,
    session: AgentSessionSnapshot,
    message_id: str,
    content: str,
    completed_at: str,
    last_event_sequence: int,
) -> AgentTurnCompletionPlan:
    """固化完整助手回答并生成 ``message.completed`` 事件。

    供应商增量只用于实时展示，**不应直接写入规范消息**；调用方在收到明确的完成
    信号后，把聚合后的可见文本与本计划一起原子持久化。

    @param content: 聚合后的完整可见文本，**必须非空**。
    @returns 回合完成计划。
    @raises AgentSessionStateError: 会话不在 ``running``。
    @raises ValueError: 内容为空或消息 ID 已存在。
    """
    if session.status != "running":
        raise AgentSessionStateError(session.status, "idle")
    resolved_id = _require_text(message_id, "Agent 助手消息 ID")
    # 成功回合必须有内容：空内容应走失败或取消路径。
    resolved_content = _require_text(content, "Agent 助手消息")
    _require_new_message(session, resolved_id)
    completed = _require_text(completed_at, "Agent 回合完成时间")

    assistant_message = NormalizedAgentMessage(
        id=resolved_id, role="assistant", content=resolved_content,
        created_at=completed, status="completed", error_code=None,
    )
    idle = transition_agent_session(session, "idle")
    next_session = replace(idle, messages=(*idle.messages, assistant_message))
    event = AgentEventSequencer(session.id, last_event_sequence).next(
        event_type="message.completed",
        message_id=resolved_id,
        occurred_at=completed,
        payload={
            "role": "assistant",
            "provider": session.provider,
            "ledger_version": session.ledger_version,
        },
    )
    return AgentTurnCompletionPlan(
        session=next_session, assistant_message=assistant_message, event=event
    )


def fail_agent_turn(
    *,
    session: AgentSessionSnapshot,
    message_id: str,
    outcome: AgentTurnFailureOutcome,
    partial_content: str | None,
    finished_at: str,
    last_event_sequence: int,
) -> AgentTurnFailurePlan:
    """固化失败或取消并恢复到 ``idle`` 回合边界。

    ``failed`` 只能收口 ``running``；``cancelled`` 只能收口已经进入 ``cancelling``
    的会话——取消必须经过那个中间态，否则无法区分"完成后正常结束"与"被取消"。

    @param partial_content: 供应商已经产生的用户可见文本；空则不生成消息。
    @returns 回合失败计划。
    @raises AgentSessionStateError: 会话状态与终态不匹配。
    @raises ValueError: 参数为空或消息 ID 已存在。
    """
    expected = "running" if outcome == "failed" else "cancelling"
    if session.status != expected:
        raise AgentSessionStateError(session.status, "idle")
    resolved_id = _require_text(message_id, "Agent 助手消息 ID")
    _require_new_message(session, resolved_id)
    finished = _require_text(finished_at, "Agent 回合结束时间")

    error_code = TURN_FAILED_CODE if outcome == "failed" else TURN_CANCELLED_CODE
    # 空的部分回答**不生成消息**：凭空造一条会让历史里出现"模型说过话但什么都没说"。
    partial = (partial_content or "").strip()
    assistant_message = (
        None
        if partial == ""
        else NormalizedAgentMessage(
            id=resolved_id, role="assistant", content=partial,
            created_at=finished, status=outcome, error_code=error_code,
        )
    )
    idle = transition_agent_session(session, "idle")
    next_session = replace(
        idle,
        messages=idle.messages if assistant_message is None else (*idle.messages, assistant_message),
    )
    event = AgentEventSequencer(session.id, last_event_sequence).next(
        event_type="message.failed",
        message_id=resolved_id,
        occurred_at=finished,
        payload={
            "provider": session.provider,
            "ledger_version": session.ledger_version,
            "outcome": outcome,
            "error_code": error_code,
        },
    )
    return AgentTurnFailurePlan(
        session=next_session, assistant_message=assistant_message, event=event
    )


def _require_new_message(session: AgentSessionSnapshot, message_id: str) -> None:
    """确认消息 ID 未在当前会话中出现。

    @raises ValueError: ID 已存在。
    """
    if any(message.id == message_id for message in session.messages):
        raise ValueError("Agent 消息 ID 已存在")


def _require_text(value: str, label: str) -> str:
    """规范文本并拒绝空值。"""
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{label}不能为空")
    return normalized
