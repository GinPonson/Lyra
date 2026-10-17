"""流式状态机测试。

最要紧的一条是 :func:`test_reasoning_never_enters_state_or_events`：
**模型隐藏推理的原文既不写入状态、也不发事件**。若写入状态，它就会随重连缓冲
回放给用户——而缓冲的用途是"让客户端接上断点"，不是记录模型在想什么。
"""

from __future__ import annotations

import pytest

from lyra.domain.agent.switching import AgentSessionSnapshot
from lyra.domain.agent.turn import TURN_FAILED_CODE
from lyra.domain.agent.turn_stream import (
    MessageDelta,
    MessageFailed,
    MessageSnapshot,
    ReasoningDelta,
    TurnSettled,
    UsageUpdated,
    apply_agent_turn_stream_signal,
    create_agent_turn_stream_state,
    finalize_agent_turn_stream,
)
from lyra.domain.analytics.types import AccountScope


def _session(status: str = "running") -> AgentSessionSnapshot:
    return AgentSessionSnapshot(
        id="s-1", provider="genkit", status=status,  # type: ignore[arg-type]
        account_scope=AccountScope(kind="all"), ledger_version=3, as_of="t",
        revision=1, summary=None, messages=(),
    )


def _state(status: str = "running"):
    return create_agent_turn_stream_state(
        session=_session(status), message_id="a-1", last_event_sequence=0
    )


def _apply(state, signal, session=None, occurred_at="2026-10-12T09:00:01.000Z"):
    return apply_agent_turn_stream_signal(
        state=state, session=session or _session(), signal=signal, occurred_at=occurred_at
    )


# --------------------------------------------------------------------------
# 建立
# --------------------------------------------------------------------------


def test_stream_requires_running_session() -> None:
    """只有 running 会话可以建立消息流。"""
    for status in ("idle", "cancelling", "closed"):
        with pytest.raises(ValueError, match="只有 running 会话"):
            _state(status)


def test_initial_state_is_empty() -> None:
    """初始状态没有内容、没有快照、未 settled。"""
    state = _state()

    assert state.visible_content == ""
    assert state.has_authoritative_snapshot is False
    assert state.settled is False
    assert state.failure_outcome is None
    assert state.total_tokens is None


# --------------------------------------------------------------------------
# 文本增量
# --------------------------------------------------------------------------


def test_text_deltas_accumulate_and_emit() -> None:
    """文本增量累积并逐条发出。"""
    state = _state()

    first = _apply(state, MessageDelta(delta="本月"))
    second = _apply(first.state, MessageDelta(delta="净收益 200"))

    assert second.state.visible_content == "本月净收益 200"
    assert first.event is not None and first.event.payload["text"] == "本月"
    assert second.event is not None and second.event.sequence == 2


def test_empty_delta_emits_nothing() -> None:
    """空增量不发事件、不改状态。"""
    update = _apply(_state(), MessageDelta(delta=""))

    assert update.event is None
    assert update.state.visible_content == ""


def test_snapshot_overrides_accumulated_deltas() -> None:
    """**权威快照覆盖增量聚合**，而不是追加——否则会产生重复文本。"""
    state = _state()
    state = _apply(state, MessageDelta(delta="本月净收益 2")).state
    update = _apply(state, MessageSnapshot(content="本月净收益 200", stop_reason="stop"))

    assert update.state.visible_content == "本月净收益 200"
    assert update.state.has_authoritative_snapshot is True
    assert update.event is None


def test_delta_after_snapshot_is_rejected() -> None:
    """快照之后不能再追加增量。"""
    state = _apply(_state(), MessageSnapshot(content="x", stop_reason="stop")).state

    with pytest.raises(ValueError, match="不能追加文本增量"):
        _apply(state, MessageDelta(delta="y"))


# --------------------------------------------------------------------------
# 隐藏推理（安全性质）
# --------------------------------------------------------------------------


def test_reasoning_never_enters_state_or_events() -> None:
    """**模型隐藏推理的原文不得进入状态或传输层。**

    若写入状态，它会随重连缓冲回放给用户——而缓冲的用途是"接上断点"，
    不是记录模型在想什么。
    """
    secret = "用户可能是在试探我的指令，我应该先回答 X 再隐藏 Y"

    update = _apply(_state(), ReasoningDelta(delta=secret))

    # 状态里没有原文。
    assert secret not in str(update.state)
    assert update.state.visible_content == ""
    # 事件里只有"思考中"标记。
    assert update.event is not None
    assert update.event.payload == {
        "state": "active", "provider": "genkit", "ledger_version": 3
    }
    assert secret not in str(update.event)


def test_reasoning_does_not_advance_visible_content() -> None:
    """推理增量不影响可见文本。"""
    state = _apply(_state(), MessageDelta(delta="答案")).state
    update = _apply(state, ReasoningDelta(delta="再想想"))

    assert update.state.visible_content == "答案"


def test_empty_reasoning_delta_emits_nothing() -> None:
    """空推理增量不发事件。"""
    assert _apply(_state(), ReasoningDelta(delta="")).event is None


# --------------------------------------------------------------------------
# 用量
# --------------------------------------------------------------------------


def test_usage_update_is_recorded_and_emitted() -> None:
    """用量更新写入状态并发事件。"""
    update = _apply(_state(), UsageUpdated(total_tokens=120, total_cost=0.0034))

    assert update.state.total_tokens == 120
    assert update.state.total_cost == 0.0034
    assert update.event is not None
    assert update.event.payload["total_tokens"] == 120


def test_usage_allows_null_cost() -> None:
    """成本可以为空（供应商未提供）。"""
    update = _apply(_state(), UsageUpdated(total_tokens=10, total_cost=None))
    assert update.state.total_cost is None


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_usage_is_rejected(value: float) -> None:
    """用量必须是非负有限数值。"""
    with pytest.raises(ValueError, match="非负有限数值"):
        _apply(_state(), UsageUpdated(total_tokens=value, total_cost=None))


# --------------------------------------------------------------------------
# 失败与结束
# --------------------------------------------------------------------------


def test_failure_outcome_is_recorded() -> None:
    """失败终态写入状态，不立即发事件。"""
    update = _apply(_state(), MessageFailed(outcome="failed"))

    assert update.state.failure_outcome == "failed"
    assert update.event is None


def test_conflicting_failure_outcomes_are_rejected() -> None:
    """同一个流里不能出现两种失败终态。"""
    state = _apply(_state(), MessageFailed(outcome="failed")).state

    with pytest.raises(ValueError, match="冲突的失败终态"):
        _apply(state, MessageFailed(outcome="cancelled"))


def test_repeated_same_failure_is_idempotent() -> None:
    """重复的同种失败终态可以接受。"""
    state = _apply(_state(), MessageFailed(outcome="failed")).state
    update = _apply(state, MessageFailed(outcome="failed"))
    assert update.state.failure_outcome == "failed"


def test_failure_after_snapshot_is_rejected() -> None:
    """成功快照后不能改写为失败。"""
    state = _apply(_state(), MessageSnapshot(content="x", stop_reason="stop")).state

    with pytest.raises(ValueError, match="不能改写为失败"):
        _apply(state, MessageFailed(outcome="failed"))


def test_snapshot_after_failure_is_rejected() -> None:
    """失败后不能接受成功快照。"""
    state = _apply(_state(), MessageFailed(outcome="failed")).state

    with pytest.raises(ValueError, match="不能接受成功消息快照"):
        _apply(state, MessageSnapshot(content="x", stop_reason="stop"))


def test_settled_rejects_further_signals() -> None:
    """**settled 后拒绝一切新信号**，防止把两个回合的文本拼在一起。"""
    state = _apply(_state(), TurnSettled()).state
    assert state.settled is True

    for signal in (MessageDelta(delta="x"), ReasoningDelta(delta="y"),
                   MessageSnapshot(content="z", stop_reason="stop"), TurnSettled()):
        with pytest.raises(ValueError, match="已经 settled"):
            _apply(state, signal)


# --------------------------------------------------------------------------
# 终态
# --------------------------------------------------------------------------


def _settle(state):
    return _apply(state, TurnSettled()).state


def test_finalize_requires_settled() -> None:
    """未 settled 不能持久化终态。"""
    with pytest.raises(ValueError, match="尚未 settled"):
        finalize_agent_turn_stream(
            state=_state(), session=_session(), finished_at="t"
        )


def test_finalize_succeeds_with_authoritative_snapshot() -> None:
    """有权威快照且非空 → 成功完成。"""
    state = _settle(_apply(
        _state(), MessageSnapshot(content="本月净收益 200", stop_reason="stop")
    ).state)

    plan = finalize_agent_turn_stream(
        state=state, session=_session(), finished_at="2026-10-12T09:00:05.000Z"
    )

    assert plan.session.status == "idle"
    assert plan.assistant_message is not None
    assert plan.assistant_message.content == "本月净收益 200"
    assert plan.event.type == "message.completed"


def test_finalize_without_snapshot_fails_even_if_text_arrived() -> None:
    """**没有权威快照就不算成功**，即使增量收到过文本。"""
    state = _settle(_apply(_state(), MessageDelta(delta="半截回答")).state)

    plan = finalize_agent_turn_stream(state=state, session=_session(), finished_at="t")

    assert plan.session.status == "idle"
    assert plan.assistant_message is not None
    assert plan.assistant_message.status == "failed"
    assert plan.assistant_message.error_code == TURN_FAILED_CODE


def test_finalize_with_empty_snapshot_fails() -> None:
    """权威快照为空也不算成功。"""
    state = _settle(_apply(
        _state(), MessageSnapshot(content="   ", stop_reason="stop")
    ).state)

    plan = finalize_agent_turn_stream(state=state, session=_session(), finished_at="t")

    assert plan.assistant_message is None
    assert plan.event.payload["outcome"] == "failed"


def test_finalize_with_failure_outcome_uses_it() -> None:
    """供应商上报的失败终态优先，且保留已产生的部分回答。"""
    state = _settle(_apply(_state(), MessageFailed(outcome="failed")).state)

    plan = finalize_agent_turn_stream(state=state, session=_session(), finished_at="t")

    assert plan.session.status == "idle"
    assert plan.event.payload["outcome"] == "failed"
    assert plan.assistant_message is None  # 没有可见文本


def test_cancelled_outcome_needs_cancelling_session() -> None:
    """取消必须由调用方先把会话转为 cancelling——不能从供应商退出码猜用户意图。"""
    state = _settle(_apply(_state(), MessageFailed(outcome="cancelled")).state)

    plan = finalize_agent_turn_stream(
        state=state, session=_session("cancelling"), finished_at="t"
    )

    assert plan.event.payload["outcome"] == "cancelled"
    assert plan.session.status == "idle"


def test_stream_state_mismatch_is_rejected() -> None:
    """流状态与传入会话不匹配时拒绝——否则会把事件写到别的会话上。"""
    other = AgentSessionSnapshot(
        id="s-2", provider="genkit", status="running",
        account_scope=AccountScope(kind="all"), ledger_version=3, as_of="t",
        revision=1, summary=None, messages=(),
    )

    with pytest.raises(ValueError, match="不匹配"):
        _apply(_state(), MessageDelta(delta="x"), session=other)


# --------------------------------------------------------------------------
# 工具事件
# --------------------------------------------------------------------------


def _tool_started(**overrides):
    """一条已授权的工具开始信号。"""
    from lyra.domain.agent.turn_stream import ToolStarted

    fields = {
        "message_id": "u-1", "call_id": "c-1", "ordinal": 1, "tool_name": "summary",
        "authorization_decision": "allowed", "status": "started",
        "arguments_summary": {"account": "TST001"}, "result_summary": None,
        "duration_ms": None, "error_code": None, "ledger_version": 3,
    }
    fields.update(overrides)
    return ToolStarted(**fields)


def _tool_completed(**overrides):
    """一条工具终态信号。"""
    from lyra.domain.agent.turn_stream import ToolCompleted

    fields = {
        "message_id": "u-1", "call_id": "c-1", "ordinal": 1, "tool_name": "summary",
        "authorization_decision": "allowed", "status": "completed",
        "arguments_summary": {"account": "TST001"}, "result_summary": {"count": 1},
        "duration_ms": 12, "error_code": None, "ledger_version": 3,
    }
    fields.update(overrides)
    return ToolCompleted(**fields)


def test_tool_started_maps_to_a_tool_started_event() -> None:
    """工具开始映射为 ``tool.started``，序号继续推进。"""
    update = _apply(_state(), _tool_started())

    assert update.event is not None
    assert update.event.type == "tool.started"
    assert update.event.sequence == 1
    assert update.event.payload["tool_call_id"] == "c-1"
    assert update.event.payload["tool_name"] == "summary"
    assert update.state.last_event_sequence == 1
    # 工具调用挂靠已落库的用户消息，而不是本回合的助手消息。
    assert update.event.message_id == "u-1"
    assert update.event.payload["ledger_version"] == 3


def test_tool_events_do_not_carry_arguments_or_results() -> None:
    """**事件里没有参数与结果摘要**——它们属于审计面，事件会被重连缓冲回放。"""
    update = _apply(_state(), _tool_completed())

    assert update.event is not None
    assert "TST001" not in str(update.event.payload)
    assert update.event.payload["outcome"] == "completed"
    assert update.event.payload["duration_ms"] == 12
    assert update.event.payload["error_code"] is None


def test_tool_rejection_maps_to_a_completed_event_only() -> None:
    """被拒的调用只发 ``tool.completed``：它从未开始，不该有 started 事件。"""
    update = _apply(_state(), _tool_completed(
        authorization_decision="denied", status="rejected",
        result_summary=None, duration_ms=0, error_code="AGENT_TOOL_FORBIDDEN",
    ))

    assert update.event is not None
    assert update.event.type == "tool.completed"
    assert update.event.payload["outcome"] == "rejected"
    assert update.event.payload["error_code"] == "AGENT_TOOL_FORBIDDEN"


def test_tool_events_do_not_settle_or_change_content() -> None:
    """工具事件不改变可见内容，也不收口——否则回合会在工具调用处被截断。"""
    update = _apply(_state(), _tool_started())

    assert update.state.settled is False
    assert update.state.visible_content == ""
    assert update.state.has_authoritative_snapshot is False


def test_inconsistent_tool_terminal_state_is_rejected() -> None:
    """终态与授权判定必须自洽（与数据库 CHECK 同义）。"""
    with pytest.raises(ValueError, match="不一致"):
        _apply(_state(), _tool_completed(authorization_decision="denied", status="completed"))
    with pytest.raises(ValueError, match="不一致"):
        _apply(_state(), _tool_completed(authorization_decision="allowed", status="rejected"))


def test_tool_completed_cannot_be_started() -> None:
    """完成信号不能用 started——否则编排器会写出一条永远不结束的审计记录。"""
    with pytest.raises(ValueError, match="started"):
        _apply(_state(), _tool_completed(status="started"))


def test_tool_started_requires_an_allowed_decision() -> None:
    """开始信号只能是已授权的 started。"""
    with pytest.raises(ValueError, match="已授权"):
        _apply(_state(), _tool_started(authorization_decision="denied"))
