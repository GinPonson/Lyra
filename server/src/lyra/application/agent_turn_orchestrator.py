"""Agent 回合编排器。

把领域层已经验证过的部件装配成完整生命周期：

```
start_agent_turn  → 持久化 running 快照 + 用户消息 + 事件；推送 message.started
executor 产生信号  → apply_agent_turn_stream_signal；推送增量/用量事件
executor 结束      → finalize_agent_turn_stream；持久化终态；推送终态事件
```

**执行器是注入的**，因此编排逻辑可独立验证；接入 Genkit 时替换执行器即可，不必改
编排。

三处刻意的顺序约束：

1. **先持久化再执行**。若先启动模型再落库，进程崩溃会留下一条**没有任何记录的模型
   回合**——用户看到回答却查不到它的来源。
2. **事件在持久化之后才推送**。反过来会让客户端收到一个数据库中还不存在的事件，
   此时若断线重连，续传会找不到它。
3. **取消不猜意图**。取消必须由调用方先把会话转为 ``cancelling``；编排器不从执行器
   的退出码推断"用户想取消"。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from ..domain.agent.events import AgentEvent, AgentEventResume
from ..domain.agent.switching import AgentSessionSnapshot
from ..domain.agent.turn import (
    AgentTurnCompletionPlan,
    AgentTurnFailurePlan,
    AgentTurnStartPlan,
    start_agent_turn,
)
from ..domain.agent.turn_stream import (
    AgentTurnStreamSignal,
    TurnSettled,
    apply_agent_turn_stream_signal,
    create_agent_turn_stream_state,
    finalize_agent_turn_stream,
)
from .agent_event_buffer import AgentEventBuffer

# 执行器未配置时抛出的标记值。路由据此返回 503，而不是把它当成回合失败。
REGISTRY_UNAVAILABLE = "AGENT_EXECUTOR_UNAVAILABLE"


class AgentTurnExecutor(Protocol):
    """供应商执行器：产生规范化流信号。

    实现方负责与具体模型协议打交道；编排器只消费规范化信号，**不解释供应商协议**。

    ``session`` 会被传入：系统提示词需要账户范围与账本版本，而这两项属于会话状态，
    不属于调用参数。
    """

    def run(
        self, *, session: AgentSessionSnapshot, message_id: str, content: str
    ) -> Iterable[AgentTurnStreamSignal]: ...


class AgentTurnStore(Protocol):
    """回合编排所需的持久化能力。"""

    def save_session(
        self, session: AgentSessionSnapshot, *, next_event_sequence: int
    ) -> bool: ...

    def append_message(self, message: Any) -> None: ...


@dataclass
class AgentTurnRunResult:
    """一次已完成回合的编排结果。"""

    snapshot: AgentSessionSnapshot
    events: tuple[AgentEvent, ...]


@dataclass
class _Runtime:
    """单会话的内存运行态；回合结束后不保留。"""

    session: AgentSessionSnapshot
    buffer: AgentEventBuffer
    last_event_sequence: int = 0


class AgentTurnOrchestrator:
    """回合编排器。

    @param store: 持久化能力（会话快照 + 消息）。
    @param now: 服务端可信时钟。
    @param on_event: 每个事件产生时的外发钩子。
    """

    def __init__(
        self,
        *,
        store: AgentTurnStore,
        now: Callable[[], datetime],
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self._store = store
        self._now = now
        self._on_event = on_event
        self._runtimes: dict[str, _Runtime] = {}

    def register(self, session: AgentSessionSnapshot, *, last_event_sequence: int = 0) -> None:
        """登记一个会话的内存运行态。

        @param session: 会话快照。
        @param last_event_sequence: 已持久化的事件序号。
        """
        self._runtimes[session.id] = _Runtime(
            session=session,
            buffer=AgentEventBuffer(session.id, self._on_event),
            last_event_sequence=last_event_sequence,
        )

    def runtime(self, session_id: str) -> AgentSessionSnapshot | None:
        """取出会话当前快照；未登记返回 ``None``。"""
        runtime = self._runtimes.get(session_id)
        return None if runtime is None else runtime.session

    def resume_events(self, session_id: str, *, after_sequence: int) -> AgentEventResume:
        """按客户端游标生成续传计划。

        @param session_id: 会话 ID。
        @param after_sequence: 客户端已收到的最后序号。
        @returns 续传计划。
        @raises KeyError: 会话未登记。
        """
        runtime = self._runtime_for(session_id)
        return runtime.buffer.resume(
            after_sequence=after_sequence,
            last_persisted_sequence=runtime.last_event_sequence,
        )

    def subscribe(
        self, session_id: str, push: Callable[[AgentEvent], None]
    ) -> int | None:
        """订阅某会话的实时事件。

        @param session_id: 会话 ID。
        @param push: 事件回调。
        @returns 取消订阅用的句柄；会话未登记时返回 ``None``（路由据此要求客户端
            重新取快照，而不是假装订阅成功）。
        """
        runtime = self._runtimes.get(session_id)
        if runtime is None:
            return None
        return runtime.buffer.subscribe(push)

    def unsubscribe(self, session_id: str, handle: int) -> None:
        """取消订阅；会话或句柄不存在时静默返回（幂等）。"""
        runtime = self._runtimes.get(session_id)
        if runtime is not None:
            runtime.buffer.unsubscribe(handle)

    def run_turn(
        self, *, session_id: str, executor: AgentTurnExecutor, content: str
    ) -> AgentTurnRunResult:
        """执行一个完整回合。

        @param session_id: 会话 ID。
        @param executor: 供应商执行器。
        @param content: 用户内容。
        @returns 回合结束后的快照与本回合产生的事件。
        @raises KeyError: 会话未登记。
        @raises ValueError: 会话不在 ``idle``，或流状态与会话冲突。
        """
        runtime = self._runtime_for(session_id)
        started_at = _iso(self._now())
        message_id = f"{session_id}:assistant:{runtime.last_event_sequence + 1}"

        # 1) 先把 running 快照、用户消息与事件落库——不能先启动模型。
        start: AgentTurnStartPlan = start_agent_turn(
            session=runtime.session, message_id=_user_message_id(runtime),
            content=content, started_at=started_at,
            last_event_sequence=runtime.last_event_sequence,
        )
        self._advance(runtime, start.session, start.event)
        self._store.append_message(start.user_message)

        # 2) 建立流状态（必须已 running）。
        stream = create_agent_turn_stream_state(
            session=runtime.session, message_id=message_id,
            last_event_sequence=runtime.last_event_sequence,
        )
        events: list[AgentEvent] = [start.event]

        # 3) 消费执行器信号。
        for signal in executor.run(
            session=runtime.session, message_id=message_id, content=content
        ):
            update = apply_agent_turn_stream_signal(
                state=stream, session=runtime.session, signal=signal,
                occurred_at=_iso(self._now()),
            )
            stream = update.state
            if update.event is not None:
                events.append(self._record(runtime, update.event))
        if not stream.settled:
            # 执行器没有显式收口。这里**只补一个 settled**，让 finalize 按已有事实判定：
            # 有权威快照且非空 → 成功；否则 → 失败。
            #
            # 刻意**不**强行改成失败：领域层明确拒绝"成功快照后改写为失败"（那会让
            # 终态自相矛盾）。强行改成失败等于用一个更糟的错误掩盖原始问题。
            update = apply_agent_turn_stream_signal(
                state=stream, session=runtime.session, signal=TurnSettled(),
                occurred_at=_iso(self._now()),
            )
            stream = update.state

        # 4) 生成终态计划并落库。
        runtime.last_event_sequence = stream.last_event_sequence
        plan = finalize_agent_turn_stream(
            state=stream, session=runtime.session, finished_at=_iso(self._now())
        )
        self._apply_final_plan(runtime, plan, message_id)
        events.append(self._record(runtime, plan.event))
        return AgentTurnRunResult(snapshot=runtime.session, events=tuple(events))

    # -- 内部 --------------------------------------------------------------

    def _runtime_for(self, session_id: str) -> _Runtime:
        """取出运行态。

        @raises KeyError: 会话未登记。
        """
        runtime = self._runtimes.get(session_id)
        if runtime is None:
            raise KeyError(f"Agent 会话未登记: {session_id}")
        return runtime

    def _advance(
        self, runtime: _Runtime, session: AgentSessionSnapshot, event: AgentEvent
    ) -> None:
        """持久化状态变迁并推送事件。

        **事件在持久化之后才推送**：反过来会让客户端收到数据库中还不存在的事件，
        此时断线重连会找不到它。
        """
        if not self._store.save_session(session, next_event_sequence=event.sequence):
            raise ValueError("Agent 会话状态已被并发修改，请重试")
        runtime.session = session
        runtime.last_event_sequence = event.sequence
        runtime.buffer.append(event)

    def _record(self, runtime: _Runtime, event: AgentEvent) -> AgentEvent:
        """把一个已生成的事件写入缓冲并推进序号。"""
        runtime.last_event_sequence = event.sequence
        runtime.buffer.append(event)
        return event

    def _apply_final_plan(
        self, runtime: _Runtime, plan: AgentTurnCompletionPlan | AgentTurnFailurePlan,
        message_id: str,
    ) -> None:
        """落库终态会话与（可选的）助手消息。"""
        if not self._store.save_session(
            plan.session, next_event_sequence=plan.event.sequence
        ):
            raise ValueError("Agent 会话状态已被并发修改，请重试")
        runtime.session = plan.session
        if plan.assistant_message is not None:
            self._store.append_message(plan.assistant_message)
        _ = message_id


def _user_message_id(runtime: _Runtime) -> str:
    """生成用户消息 ID。

    真实实现由客户端提供消息 ID；编排器只在无客户端 ID 时兜底生成，避免同一回合
    因重试写出两条用户消息。
    """
    return f"{runtime.session.id}:user:{runtime.last_event_sequence + 1}"


def _iso(value: datetime) -> str:
    """渲染为与 JavaScript ``toISOString()`` 同形的文本。"""

    return (
        value.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
