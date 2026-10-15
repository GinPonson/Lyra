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
from threading import RLock
from typing import Any, Literal, Protocol
from uuid import uuid4

from ..domain.agent.events import AgentEvent, AgentEventResume
from ..domain.agent.lifecycle import AgentSessionStateError, transition_agent_session
from ..domain.agent.messages import AgentMessageStatus, AgentToolCall, TurnMessage
from ..domain.agent.switching import AgentSessionSnapshot
from ..domain.agent.tool_call import AgentToolContext
from ..domain.agent.turn import (
    AgentTurnCompletionPlan,
    AgentTurnFailurePlan,
    AgentTurnStartPlan,
    fail_agent_turn,
    start_agent_turn,
)
from ..domain.agent.turn_stream import (
    AgentTurnStreamSignal,
    ToolCompleted,
    ToolStarted,
    TurnSettled,
    apply_agent_turn_stream_signal,
    create_agent_turn_stream_state,
    finalize_agent_turn_stream,
)
from .agent_event_buffer import AgentEventBuffer

# 执行器未配置时抛出的标记值。路由据此返回 503，而不是把它当成回合失败。
REGISTRY_UNAVAILABLE = "AGENT_EXECUTOR_UNAVAILABLE"


class AgentTurnCancelled(Exception):
    """已持久化的取消请求中断执行器；由编排器统一生成取消终态。"""


class AgentTurnExecutor(Protocol):
    """供应商执行器：产生规范化流信号。

    实现方负责与具体模型协议打交道；编排器只消费规范化信号，**不解释供应商协议**。

    ``session`` 与 ``context`` 会被传入：系统提示词需要账户范围与账本版本，工具授权
    边界需要会话绑定，而这些都属于服务端状态，**不属于模型可控参数**。

    ``emit`` 是逐信号回调而不是"返回一个列表"：回合的增量必须能在产生时就落库与推送，
    攒完再返回会让流式在真实路径上消失，并让整个事件循环在模型调用期间停摆。
    """

    def run(
        self,
        *,
        session: AgentSessionSnapshot,
        content: str,
        context: AgentToolContext,
        emit: Callable[[AgentTurnStreamSignal], None],
    ) -> None: ...



class AgentTurnStore(Protocol):
    """回合编排所需的持久化能力。"""

    def save_session(
        self, session: AgentSessionSnapshot, *, next_event_sequence: int
    ) -> bool: ...

    def save_turn_start(
        self,
        session: AgentSessionSnapshot,
        message: TurnMessage,
        *,
        next_event_sequence: int,
    ) -> bool: ...

    def append_turn_message(self, message: TurnMessage) -> None: ...

    def append_tool_call(self, call: AgentToolCall) -> None: ...

    def complete_tool_call(self, call: AgentToolCall) -> bool: ...

    def session_status(self, session_id: str) -> str | None: ...


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
    #: 发起会话的设备；登出/撤销时据此终止该设备的在跑回合。
    device_id: str | None = None
    #: 下一条消息的序号；由已落库消息数推出，避免与既有行冲突。
    next_ordinal: int = 1
    #: 本回合的起始时间；工具审计的 ``startedAt`` 用它，而不是"信号到达时间"。
    started_at: datetime | None = None


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
        # 只保护状态转换和事件缓冲；模型网络调用不能持锁，否则取消仍会被阻塞。
        self._lock = RLock()

    def register(
        self,
        session: AgentSessionSnapshot,
        *,
        last_event_sequence: int = 0,
        device_id: str | None = None,
        next_ordinal: int = 1,
    ) -> None:
        """登记一个会话的内存运行态。

        **已登记的会话就地更新，不重建运行态。** 重建会连带换掉事件缓冲，而 WebSocket
        的订阅挂在缓冲上——每发一条消息都会静默丢掉客户端刚建立的订阅，实时事件永远
        送不到（这曾是真实缺陷）。

        @param session: 会话快照。
        @param last_event_sequence: 已持久化的事件序号。
        @param device_id: 发起会话的设备 ID。
        @param next_ordinal: 下一条消息的序号。
        """
        with self._lock:
            existing = self._runtimes.get(session.id)
            if existing is not None:
                if existing.session.status in ("running", "cancelling") or (
                    session.revision < existing.session.revision
                ):
                    raise ValueError("Agent 会话状态已被并发修改，请重试")
                # HTTP 读取快照和消息分两步进行；同 revision 时内存可能已经包含刚落库
                # 的助手消息，不能用较早读到的消息列表把完整历史覆盖掉。
                if session.revision > existing.session.revision:
                    existing.session = session
                existing.last_event_sequence = max(
                    existing.last_event_sequence, last_event_sequence
                )
                if device_id is not None:
                    existing.device_id = device_id
                existing.next_ordinal = max(existing.next_ordinal, next_ordinal)
                return
            self._runtimes[session.id] = _Runtime(
                session=session,
                buffer=AgentEventBuffer(session.id, self._on_event),
                last_event_sequence=last_event_sequence,
                device_id=device_id,
                next_ordinal=next_ordinal,
            )

    def runtime(self, session_id: str) -> AgentSessionSnapshot | None:
        """取出会话当前快照；未登记返回 ``None``。"""
        with self._lock:
            runtime = self._runtimes.get(session_id)
            return None if runtime is None else runtime.session

    def resume_events(self, session_id: str, *, after_sequence: int) -> AgentEventResume:
        """按客户端游标生成续传计划。

        @param session_id: 会话 ID。
        @param after_sequence: 客户端已收到的最后序号。
        @returns 续传计划。
        @raises KeyError: 会话未登记。
        """
        with self._lock:
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
        with self._lock:
            runtime = self._runtimes.get(session_id)
            if runtime is None:
                return None
            return runtime.buffer.subscribe(push)

    def unsubscribe(self, session_id: str, handle: int) -> None:
        """取消订阅；会话或句柄不存在时静默返回（幂等）。"""
        with self._lock:
            runtime = self._runtimes.get(session_id)
            if runtime is not None:
                runtime.buffer.unsubscribe(handle)

    def subscribe_with_resume(
        self, session_id: str, push: Callable[[AgentEvent], None], *, after_sequence: int
    ) -> tuple[int, AgentEventResume] | None:
        """原子地取得续传事件并订阅后续事件，避免工作线程在两步之间产生丢失事件。

        @param session_id: 需要订阅的会话 ID。
        @param push: 后续事件的同步回调；调用方负责调度到其事件循环。
        @param after_sequence: 客户端已经接收的最后序号。
        @returns 订阅句柄与续传计划；会话未登记时返回 None。
        """
        with self._lock:
            runtime = self._runtimes.get(session_id)
            if runtime is None:
                return None
            resume = runtime.buffer.resume(
                after_sequence=after_sequence,
                last_persisted_sequence=runtime.last_event_sequence,
            )
            return runtime.buffer.subscribe(push), resume

    def request_cancel(self, session_id: str, *, now: datetime) -> AgentSessionSnapshot | None:
        """请求取消一个在跑的回合。

        **取消必须落库**：只有 ``cancelling`` 这个持久化状态能活过进程重启，也被
        编排器在收口时读取（见 :meth:`_cancellation_requested`）。同时按领域规则做
        CAS——会话不是 ``running`` 时返回 ``None``，由调用方决定报什么错。

        @param session_id: 会话 ID。
        @param now: 服务端时钟。
        @returns 推进后的快照；会话未登记或不在 ``running`` 时返回 ``None``。
        """
        with self._lock:
            runtime = self._runtimes.get(session_id)
            if runtime is None or runtime.session.status != "running":
                return None
            try:
                cancelling = transition_agent_session(runtime.session, "cancelling")
            except AgentSessionStateError:
                return None
            if not self._store.save_session(
                cancelling, next_event_sequence=runtime.last_event_sequence
            ):
                return None
            runtime.session = cancelling
            return cancelling

    def cancel_device_turns(self, device_id: str, *, now: datetime) -> tuple[str, ...]:
        """请求取消某设备所有在跑的回合。

        **退出登录必须能终止该设备的行为**：设备撤销后如果回合继续在服务端跑完并落库，
        "已登出"就只是本地状态，服务端仍然替它产生并保存了回答。

        @param device_id: 已撤销的设备 ID。
        @param now: 服务端时钟。
        @returns 被请求取消的会话 ID。
        """
        with self._lock:
            cancelled: list[str] = []
            for session_id, runtime in list(self._runtimes.items()):
                if runtime.device_id != device_id or runtime.session.status != "running":
                    continue
                if self.request_cancel(session_id, now=now) is not None:
                    cancelled.append(session_id)
            return tuple(cancelled)

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
        with self._lock:
            runtime = self._runtime_for(session_id)
            started_at = _iso(self._now())
            runtime.started_at = self._now()
            # **助手消息 ID 必须是 UUID**：``agent_messages.id`` 是 UUID 列，写入时
            # ``CAST(? AS UUID)``；拼出来的 ``"<session>:assistant:N"`` 会让插入直接失败。
            # 它同时是客户端看到的 ``messageId``，也必须符合公开的 UUID 约定。
            message_id = str(uuid4())

            # 1) 先把 running 快照、用户消息与事件**原子地**落库——不能先启动模型。
            #
            # 会话快照与用户消息必须在同一事务里：分散成两条语句时，后一条失败会留下
            # "会话已 running、却没有用户消息"的半写状态，而 ``send_message`` 只在
            # ``idle`` 才受理，``DELETE`` 又不允许 running→closed——会话会永久卡住，
            # 只能手工改库。
            start: AgentTurnStartPlan = start_agent_turn(
                session=runtime.session, message_id=str(uuid4()),
                content=content, started_at=started_at,
                last_event_sequence=runtime.last_event_sequence,
            )
            user_message = _turn_message(
                runtime, start.user_message, role="user", status="completed",
                completed_at=started_at,
            )
            if not self._store.save_turn_start(
                start.session, user_message, next_event_sequence=start.event.sequence
            ):
                raise ValueError("Agent 会话状态已被并发修改，请重试")
            runtime.session = start.session
            runtime.next_ordinal = user_message.ordinal + 1
            runtime.last_event_sequence = start.event.sequence
            runtime.buffer.append(start.event)

            # 2) 建立流状态（必须已 running）。
            stream = create_agent_turn_stream_state(
                session=runtime.session, message_id=message_id,
                last_event_sequence=runtime.last_event_sequence,
            )
            events: list[AgentEvent] = [start.event]

        # 3) 消费执行器信号。
        #
        # **逐信号处理而不是先把信号收进列表**：收完再处理会让流式在真实路径上消失，
        # 也会让"取消"只能等模型全部返回之后才有机会生效。
        tool_ordinal = 0

        def consume(signal: AgentTurnStreamSignal) -> None:
            nonlocal stream, tool_ordinal
            with self._lock:
                if runtime.session.status == "cancelling" and not isinstance(
                    signal, (ToolCompleted, TurnSettled)
                ):
                    raise AgentTurnCancelled()
                # **工具审计与事件同时落库**：``tool.started`` / ``tool.completed`` 事件
                # 只是实时通知，审计行才是事后可查的记录。两个写在一起，客户端看到的
                # 与本轮结束后查到的才是同一批调用。
                if isinstance(signal, ToolStarted):
                    tool_ordinal += 1
                    self._store.append_tool_call(_tool_call(
                        runtime, signal, ordinal=tool_ordinal, status="started",
                        result_summary=None, completed_at=None, session_id=runtime.session.id,
                    ))
                elif isinstance(signal, ToolCompleted):
                    if signal.status == "rejected":
                        # **被拒绝的调用从未 started**：仓储的 ``complete_tool_call`` 只更新
                        # ``status='started'`` 的行，用它去收口一条不存在的行会静默丢掉这次
                        # 尝试——而"模型试过什么"恰恰是审计最需要留下的一条。因此这里直接
                        # 插入终态行，而不是先插 started 再更新。
                        tool_ordinal += 1
                        self._store.append_tool_call(_tool_call(
                            runtime, signal, ordinal=tool_ordinal,
                            status=signal.status, result_summary=signal.result_summary,
                            completed_at=_iso(self._now()), session_id=runtime.session.id,
                        ))
                    else:
                        self._store.complete_tool_call(_tool_call(
                            runtime, signal, ordinal=tool_ordinal,
                            status=signal.status, result_summary=signal.result_summary,
                            completed_at=_iso(self._now()), session_id=runtime.session.id,
                        ))
                update = apply_agent_turn_stream_signal(
                    state=stream, session=runtime.session, signal=signal,
                    occurred_at=_iso(self._now()),
                )
                stream = update.state
                if update.event is not None:
                    events.append(self._record(runtime, update.event))

        try:
            executor.run(
                session=runtime.session, content=content,
                # ``user_message_id`` 必须一起注入：工具审计行有
                # ``(session_id, message_id) → agent_messages`` 的外键，而助手消息要到
                # 回合末才落库，能安全引用的只有刚写好的用户消息。
                context=_turn_context(runtime, user_message_id=user_message.id),
                emit=consume,
            )
        except AgentTurnCancelled:
            # 取消已经落库，终态继续走下面统一的持久化路径。
            pass
        with self._lock:
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
            if self._cancellation_requested(runtime):
                # **用户已经点了"停止"**：按 ``cancelled`` 收口，而不是把模型跑完的结果
                # 记成一次成功。判断依据是**持久化状态**（``cancelling``），不是进程内的
                # 标志位——重启后进程内状态会丢，而取消意图必须活过重启。
                plan = fail_agent_turn(
                    session=runtime.session, message_id=message_id, outcome="cancelled",
                    partial_content=stream.visible_content,
                    finished_at=_iso(self._now()),
                    last_event_sequence=runtime.last_event_sequence,
                )
            else:
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

    def _cancellation_requested(self, runtime: _Runtime) -> bool:
        """会话是否已被请求取消。

        **读库而不是读内存标志**：取消请求可能来自另一个线程（同步路由跑在线程池里）
        或另一次进程生命周期，只有 ``cancelling`` 这个持久化状态是可信来源。
        """
        return self._store.session_status(runtime.session.id) == "cancelling"

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
            # 终态消息的 ``status`` 与用户可见的部分内容都由领域层决定；这里只补
            # 持久化需要的归属、序号、供应商与完成时间。
            self._store.append_turn_message(_turn_message(
                runtime, plan.assistant_message, role="assistant",
                status=_terminal_status(plan),
                completed_at=plan.assistant_message.created_at,
                message_id=message_id,
            ))
            runtime.next_ordinal += 1
        _ = message_id


def _terminal_status(plan: AgentTurnCompletionPlan | AgentTurnFailurePlan) -> AgentMessageStatus:
    """助手消息的持久终态。

    成功是 ``completed``；失败/取消沿用领域层给出的 ``status``（``failed`` /
    ``cancelled``）——它是客户端的唯一依据。
    """
    if isinstance(plan, AgentTurnCompletionPlan):
        return "completed"
    status = plan.assistant_message.status if plan.assistant_message is not None else None
    return "cancelled" if status == "cancelled" else "failed"


def _turn_message(
    runtime: _Runtime,
    message: Any,
    *,
    role: str,
    status: AgentMessageStatus,
    completed_at: str | None,
    message_id: str | None = None,
) -> TurnMessage:
    """把领域消息补成可落库的回合消息。

    **序号由运行态给出**，不由数据层推导：判定 ``ordinal`` 需要在写事务里查一次
    ``max(ordinal)``，而运行态在登记时已经拿过消息列表。
    """
    return TurnMessage(
        id=message_id or message.id,
        session_id=runtime.session.id,
        ordinal=runtime.next_ordinal,
        role=role,
        provider=runtime.session.provider,
        status=status,
        content=message.content,
        error_code=message.error_code if isinstance(message.error_code, str) else None,
        created_at=message.created_at,
        completed_at=completed_at,
    )


def _turn_context(runtime: _Runtime, *, user_message_id: str) -> AgentToolContext:
    """服务端注入的回合上下文。

    **它不是模型可控参数**：账户范围、账本版本与数据截止时间都来自会话，模型只能
    在其中活动，不能自己指定——否则一句提示词注入就能把查询扩到全部账户。

    ``user_message_id`` 一并给出，是因为工具审计行有
    ``(session_id, message_id) → agent_messages`` 的外键，而助手消息要到回合末
    才落库；能安全引用的只有刚写好的用户消息。
    """
    return AgentToolContext(
        session_id=runtime.session.id,
        # 未登记设备时用空串：该字段只用于归属与排查，不是授权依据。
        device_id=runtime.device_id or "",
        user_message_id=user_message_id,
        account_scope=runtime.session.account_scope,
        ledger_version=runtime.session.ledger_version,
        as_of=runtime.session.as_of,
    )


def _tool_call(
    runtime: _Runtime,
    signal: ToolStarted | ToolCompleted,
    *,
    ordinal: int,
    status: str,
    result_summary: Any,
    completed_at: str | None,
    session_id: str,
) -> AgentToolCall:
    """把工具信号映射为审计行。

    摘要来自信号（已由领域层净化过）：原始参数与第三方响应**不得**进入审计表。
    """
    return AgentToolCall(
        id=signal.call_id,
        session_id=session_id,
        message_id=signal.message_id,
        ordinal=ordinal,
        tool_name=signal.tool_name,
        authorization_decision=signal.authorization_decision,
        status=status,  # type: ignore[arg-type]
        arguments_summary=dict(signal.arguments_summary),
        result_summary=None if result_summary is None else dict(result_summary),
        ledger_version=signal.ledger_version,
        duration_ms=signal.duration_ms,
        error_code=signal.error_code,
        started_at=_iso(runtime.started_at),
        completed_at=completed_at,
    )


def _iso(value: datetime) -> str:
    """渲染为与 JavaScript ``toISOString()`` 同形的文本。"""

    return (
        value.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
