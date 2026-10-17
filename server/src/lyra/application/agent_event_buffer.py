"""会话事件的有界缓冲与订阅分发。

抽自 TypeScript 版 ``agent-turn-orchestrator.ts`` 的基础设施部分。

**为什么必须有界**：事件只在内存里供 WebSocket 续传用，不持久化正文。若缓冲无界，
一个长会话会把整个进程的内存吃光——而它服务的是单个设备。

**淘汰最旧事件的直接后果**：旧续传游标会得到 ``snapshot_required``（缓冲区不再
覆盖到它需要的区间）。这不是缺陷，而是正确的降级——**客户端重新取一次权威快照，
比服务端凭残存缓冲拼出错误的增量安全得多**。
"""

from __future__ import annotations

from collections.abc import Callable

from ..domain.agent.events import (
    AgentEvent,
    AgentEventResume,
    plan_agent_event_resume,
)

# 事件缓冲上限；溢出后丢弃最旧事件，续传方会收到 snapshot_required。
MAX_BUFFERED_EVENTS = 500


class AgentEventBuffer:
    """单个会话的有界事件缓冲。

    @param session_id: 会话 ID。
    @param on_event: 每个事件产生时的外发钩子（例如推给 WebSocket）。
    """

    def __init__(
        self,
        session_id: str,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        if session_id.strip() == "":
            raise ValueError("会话 ID 不能为空")
        self._session_id = session_id
        self._events: list[AgentEvent] = []
        self._on_event = on_event
        # 订阅者用 id 作键，避免把可调用对象放进集合时因哈希语义出意外。
        self._subscribers: dict[int, Callable[[AgentEvent], None]] = {}

    @property
    def buffered_count(self) -> int:
        """当前缓冲的事件数。"""
        return len(self._events)

    @property
    def subscriber_count(self) -> int:
        """当前订阅者数量。

        用于验证"被拒绝的订阅确实没有建立"：归属校验不通过时若仍注册了订阅者，事件会
        照常推给他——只看响应帧是看不出这一点的。
        """
        return len(self._subscribers)

    def append(self, event: AgentEvent) -> None:
        """追加事件，超出上限时丢弃最旧的一条。

        @param event: 待分发的事件。
        @raises ValueError: 事件属于其它会话——混入会让续传判定失效。
        """
        if event.session_id != self._session_id:
            raise ValueError("不能把其它会话的事件写入本会话缓冲")
        self._events.append(event)
        if len(self._events) > MAX_BUFFERED_EVENTS:
            # 丢弃最旧：新事件才是客户端正在等待的。
            self._events.pop(0)
        if self._on_event is not None:
            self._on_event(event)
        for push in list(self._subscribers.values()):
            push(event)

    def subscribe(self, subscriber: Callable[[AgentEvent], None]) -> int:
        """注册订阅者。

        @param subscriber: 事件回调。
        @returns 取消订阅用的句柄。
        """
        handle = id(subscriber)
        self._subscribers[handle] = subscriber
        return handle

    def unsubscribe(self, handle: int) -> None:
        """取消订阅；句柄不存在时静默返回（幂等）。"""
        self._subscribers.pop(handle, None)

    def resume(self, *, after_sequence: int, last_persisted_sequence: int) -> AgentEventResume:
        """按客户端游标生成续传计划。

        缓冲区被淘汰后，旧游标会得到 ``snapshot_required``——**这是正确的降级**：
        客户端重新取一次权威快照，比服务端凭残存缓冲拼出错误增量安全得多。

        @param after_sequence: 客户端已收到的最后序号。
        @param last_persisted_sequence: 服务端已持久化的最后序号。
        @returns 续传计划。
        """
        return plan_agent_event_resume(
            session_id=self._session_id,
            after_sequence=after_sequence,
            last_persisted_sequence=last_persisted_sequence,
            buffered_events=tuple(self._events),
        )
