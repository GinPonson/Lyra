"""Agent 事件序号与重连续传测试。

**核心原则：缺口绝不补造。** 事件是增量的（`message.delta` 只带新增文本）；
若服务端凭空补一条"看起来合理"的 delta，客户端会把不完整的文本当成最终答案
——**而没有任何一方能察觉**。

因此续传失败一律要求重新取规范快照，有专门测试覆盖三种失败原因。
"""

from __future__ import annotations

import pytest

from lyra.domain.agent.events import (
    AGENT_EVENT_TYPES,
    AgentEvent,
    AgentEventSequencer,
    are_agent_events_contiguous,
    plan_agent_event_resume,
)

SESSION = "s-1"


def _event(sequence: int, session_id: str = SESSION) -> AgentEvent:
    return AgentEvent(
        type="message.delta", session_id=session_id, message_id="m-1",
        sequence=sequence, occurred_at="t", payload={"text": f"chunk{sequence}"},
    )


# --------------------------------------------------------------------------
# 序号分配
# --------------------------------------------------------------------------


def test_sequencer_starts_at_one() -> None:
    """新会话序号从 1 开始。"""
    event = AgentEventSequencer(SESSION).next(
        event_type="session.started", message_id=None, occurred_at="t", payload={}
    )
    assert event.sequence == 1
    assert event.session_id == SESSION


def test_sequencer_continues_from_persisted_sequence() -> None:
    """恢复时可用已持久化的最后序号初始化，继续递增。"""
    sequencer = AgentEventSequencer(SESSION, last_sequence=7)
    event = sequencer.next(
        event_type="message.delta", message_id="m-1", occurred_at="t", payload={}
    )
    assert event.sequence == 8


def test_sequencer_is_monotonic() -> None:
    """连续分配严格递增。"""
    sequencer = AgentEventSequencer(SESSION)
    sequences = [
        sequencer.next(
            event_type="message.delta", message_id=None, occurred_at="t", payload={}
        ).sequence
        for _ in range(5)
    ]
    assert sequences == [1, 2, 3, 4, 5]


def test_sequencer_rejects_invalid_construction() -> None:
    """空会话 ID 与非法初始序号必须拒绝。"""
    with pytest.raises(ValueError, match="sessionId 不能为空"):
        AgentEventSequencer("   ")
    for bad in (-1, 1.5, True):
        with pytest.raises(ValueError, match="非负安全整数"):
            AgentEventSequencer(SESSION, last_sequence=bad)  # type: ignore[arg-type]


def test_empty_timestamp_is_rejected() -> None:
    """空时间戳必须拒绝。"""
    with pytest.raises(ValueError, match="时间戳不能为空"):
        AgentEventSequencer(SESSION).next(
            event_type="message.delta", message_id=None, occurred_at="  ", payload={}
        )


def test_event_types_are_the_expected_eleven() -> None:
    """事件类型是闭集，共 11 种；增减都要同步客户端。"""
    assert len(AGENT_EVENT_TYPES) == 11
    assert "message.delta" in AGENT_EVENT_TYPES
    assert "session.completed" in AGENT_EVENT_TYPES


# --------------------------------------------------------------------------
# 相邻连续性
# --------------------------------------------------------------------------


def test_contiguous_events() -> None:
    """同会话且序号 +1 视为连续。"""
    assert are_agent_events_contiguous(_event(1), _event(2)) is True


def test_non_contiguous_events() -> None:
    """跨会话或跳号都不连续。"""
    assert are_agent_events_contiguous(_event(1), _event(3)) is False
    assert are_agent_events_contiguous(_event(1), _event(2, session_id="other")) is False


# --------------------------------------------------------------------------
# 续传
# --------------------------------------------------------------------------


def test_resumes_with_no_new_events_when_caught_up() -> None:
    """已追平：返回空增量，而不是要求快照。"""
    plan = plan_agent_event_resume(
        session_id=SESSION, after_sequence=5, last_persisted_sequence=5,
        buffered_events=(),
    )

    assert plan.mode == "events"
    assert plan.events == ()


def test_resumes_contiguous_tail() -> None:
    """缓冲区完整覆盖时返回增量。"""
    plan = plan_agent_event_resume(
        session_id=SESSION, after_sequence=2, last_persisted_sequence=4,
        buffered_events=(_event(1), _event(2), _event(3), _event(4)),
    )

    assert plan.mode == "events"
    assert [event.sequence for event in plan.events] == [3, 4]


def test_client_ahead_requires_snapshot() -> None:
    """客户端游标超前：两边状态已不可信。"""
    plan = plan_agent_event_resume(
        session_id=SESSION, after_sequence=9, last_persisted_sequence=4,
        buffered_events=(),
    )

    assert plan.mode == "snapshot_required"
    assert plan.reason == "client_ahead"


def test_mixed_session_requires_snapshot() -> None:
    """缓冲区混入其它会话的事件：绝不能拼接。"""
    plan = plan_agent_event_resume(
        session_id=SESSION, after_sequence=1, last_persisted_sequence=3,
        buffered_events=(_event(2), _event(3, session_id="other")),
    )

    assert plan.mode == "snapshot_required"
    assert plan.reason == "mixed_session"


def test_gap_in_the_middle_requires_snapshot() -> None:
    """**中间缺号必须要求快照**，不能跳过缺口继续发。"""
    plan = plan_agent_event_resume(
        session_id=SESSION, after_sequence=1, last_persisted_sequence=4,
        buffered_events=(_event(2), _event(4)),
    )

    assert plan.mode == "snapshot_required"
    assert plan.reason == "event_gap"


def test_buffer_too_short_requires_snapshot() -> None:
    """缓冲区没覆盖到已持久化的末尾——缺的正是最后一段。"""
    plan = plan_agent_event_resume(
        session_id=SESSION, after_sequence=1, last_persisted_sequence=4,
        buffered_events=(_event(2), _event(3)),
    )

    assert plan.mode == "snapshot_required"
    assert plan.reason == "event_gap"


def test_buffer_is_filtered_to_the_requested_window() -> None:
    """窗口之外的事件（已收到的与尚未持久化的）都要被排除。"""
    plan = plan_agent_event_resume(
        session_id=SESSION, after_sequence=2, last_persisted_sequence=3,
        buffered_events=(_event(1), _event(2), _event(3), _event(4)),
    )

    assert plan.mode == "events"
    assert [event.sequence for event in plan.events] == [3]


@pytest.mark.parametrize("bad", [-1, 1.5, True])
def test_invalid_cursors_are_rejected(bad: object) -> None:
    """游标必须是非负整数。"""
    with pytest.raises(ValueError, match="非负安全整数"):
        plan_agent_event_resume(
            session_id=SESSION, after_sequence=bad, last_persisted_sequence=1,  # type: ignore[arg-type]
            buffered_events=(),
        )


def test_empty_session_id_is_rejected() -> None:
    """空会话 ID 必须拒绝。"""
    with pytest.raises(ValueError, match="sessionId 不能为空"):
        plan_agent_event_resume(
            session_id="  ", after_sequence=0, last_persisted_sequence=0,
            buffered_events=(),
        )


def test_resume_from_zero_covers_the_whole_buffer() -> None:
    """从 0 开始续传应返回全部已持久化事件。"""
    plan = plan_agent_event_resume(
        session_id=SESSION, after_sequence=0, last_persisted_sequence=3,
        buffered_events=(_event(1), _event(2), _event(3)),
    )

    assert plan.mode == "events"
    assert [event.sequence for event in plan.events] == [1, 2, 3]
