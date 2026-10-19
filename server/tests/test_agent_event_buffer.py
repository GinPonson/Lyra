"""事件缓冲与订阅测试。

两条要点：

1. **缓冲必须有界**——事件只在内存里供 WebSocket 续传，若缓冲无界，一个长会话会
   把进程内存吃光，而它服务的是单个设备；
2. **淘汰最旧事件会让旧游标得到 `snapshot_required`**。这不是缺陷，而是正确的降级：
   客户端重新取一次权威快照，比服务端凭残存缓冲拼出错误的增量安全得多。
"""

from __future__ import annotations

import pytest

from lyra.application.agent_event_buffer import (
    MAX_BUFFERED_EVENTS,
    AgentEventBuffer,
)
from lyra.domain.agent.events import AgentEvent

SESSION = "s-1"


def _event(sequence: int, session_id: str = SESSION) -> AgentEvent:
    return AgentEvent(
        type="message.delta", session_id=session_id, message_id="m-1",
        sequence=sequence, occurred_at="t", payload={"text": f"c{sequence}"},
    )


def test_appends_and_counts() -> None:
    """追加后计数正确。"""
    buffer = AgentEventBuffer(SESSION)
    buffer.append(_event(1))
    buffer.append(_event(2))

    assert buffer.buffered_count == 2


def test_buffer_is_bounded_and_drops_the_oldest() -> None:
    """**超出上限时丢弃最旧事件**，而不是无界增长。"""
    buffer = AgentEventBuffer(SESSION)
    for sequence in range(1, MAX_BUFFERED_EVENTS + 11):
        buffer.append(_event(sequence))

    assert buffer.buffered_count == MAX_BUFFERED_EVENTS

    # 保留的是最新的那一批：最旧的已被丢弃。
    plan = buffer.resume(after_sequence=0, last_persisted_sequence=MAX_BUFFERED_EVENTS + 10)
    assert plan.mode == "snapshot_required"
    assert plan.reason == "event_gap"


def test_cursor_inside_the_evicted_region_requires_snapshot() -> None:
    """游标落在**已被淘汰**的区间内 → 必须重取快照。

    缓冲区此时只保留最后 500 条；客户端需要的 1..4 已经不在，服务端拼不出
    完整增量，只能让它重新取权威快照。
    """
    buffer = AgentEventBuffer(SESSION)
    for sequence in range(1, MAX_BUFFERED_EVENTS + 5):
        buffer.append(_event(sequence))

    plan = buffer.resume(after_sequence=0, last_persisted_sequence=4)

    assert plan.mode == "snapshot_required"
    assert plan.reason == "event_gap"


def test_cursor_exactly_at_the_buffer_window_resumes() -> None:
    """游标正好落在缓冲区起点之前一条 → 完整覆盖，可续传。"""
    buffer = AgentEventBuffer(SESSION)
    for sequence in range(1, MAX_BUFFERED_EVENTS + 5):
        buffer.append(_event(sequence))

    # 缓冲区保留 5..504；客户端收到 4，正好可以从 5 接上。
    plan = buffer.resume(after_sequence=4, last_persisted_sequence=MAX_BUFFERED_EVENTS + 4)

    assert plan.mode == "events"
    assert plan.events[0].sequence == 5
    assert plan.events[-1].sequence == MAX_BUFFERED_EVENTS + 4


def test_cursor_before_the_buffer_window_requires_snapshot() -> None:
    """游标落在缓冲区窗口之前 → 缺口，必须重取快照。"""
    buffer = AgentEventBuffer(SESSION)
    for sequence in range(1, MAX_BUFFERED_EVENTS + 5):
        buffer.append(_event(sequence))

    # 客户端只收到 1，而缓冲区从 5 开始：2..4 已不在。
    plan = buffer.resume(after_sequence=1, last_persisted_sequence=4)

    assert plan.mode == "snapshot_required"
    assert plan.reason == "event_gap"


def test_contiguous_window_resumes() -> None:
    """窗口完整覆盖时可续传。"""
    buffer = AgentEventBuffer(SESSION)
    for sequence in range(1, 6):
        buffer.append(_event(sequence))

    plan = buffer.resume(after_sequence=2, last_persisted_sequence=5)

    assert plan.mode == "events"
    assert [event.sequence for event in plan.events] == [3, 4, 5]


def test_foreign_session_event_is_rejected() -> None:
    """混入其它会话的事件会让续传判定失效，必须拒绝。"""
    buffer = AgentEventBuffer(SESSION)

    with pytest.raises(ValueError, match="其它会话的事件"):
        buffer.append(_event(1, session_id="other"))


def test_empty_session_id_is_rejected() -> None:
    """空会话 ID 必须拒绝。"""
    with pytest.raises(ValueError, match="会话 ID 不能为空"):
        AgentEventBuffer("   ")


# --------------------------------------------------------------------------
# 订阅
# --------------------------------------------------------------------------


def test_subscribers_receive_events() -> None:
    """订阅者收到此后产生的事件。"""
    buffer = AgentEventBuffer(SESSION)
    received: list[int] = []
    buffer.subscribe(lambda event: received.append(event.sequence))

    buffer.append(_event(1))
    buffer.append(_event(2))

    assert received == [1, 2]


def test_multiple_subscribers_all_receive() -> None:
    """多个订阅者各自收到。"""
    buffer = AgentEventBuffer(SESSION)
    first: list[int] = []
    second: list[int] = []
    buffer.subscribe(lambda event: first.append(event.sequence))
    buffer.subscribe(lambda event: second.append(event.sequence))

    buffer.append(_event(1))

    assert first == [1] and second == [1]


def test_unsubscribe_stops_delivery() -> None:
    """取消订阅后不再收到。"""
    buffer = AgentEventBuffer(SESSION)
    received: list[int] = []
    handle = buffer.subscribe(lambda event: received.append(event.sequence))

    buffer.append(_event(1))
    buffer.unsubscribe(handle)
    buffer.append(_event(2))

    assert received == [1]


def test_unsubscribe_is_idempotent() -> None:
    """重复取消不报错。"""
    buffer = AgentEventBuffer(SESSION)
    handle = buffer.subscribe(lambda event: None)
    buffer.unsubscribe(handle)
    buffer.unsubscribe(handle)


def test_on_event_hook_fires() -> None:
    """外发钩子在每个事件上触发。"""
    seen: list[int] = []
    buffer = AgentEventBuffer(SESSION, on_event=lambda event: seen.append(event.sequence))

    buffer.append(_event(1))

    assert seen == [1]
