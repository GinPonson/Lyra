"""回合编排器测试（执行器与仓储均为注入的假实现）。

三条顺序约束是重点：

1. **先持久化再执行**——否则进程崩溃会留下一条**没有任何记录的模型回合**；
2. **事件在持久化之后才推送**——否则断线重连时客户端会去找一个数据库里不存在的事件；
3. **取消不猜意图**——编排器不从执行器退出码推断"用户想取消"。

另：执行器**必须显式收口**。缺失收口信号时按失败处理，而不是假装成功。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lyra.application.agent_turn_orchestrator import AgentTurnOrchestrator
from lyra.domain.agent.switching import AgentSessionSnapshot
from lyra.domain.agent.turn import TURN_FAILED_CODE
from lyra.domain.agent.turn_stream import (
    MessageDelta,
    MessageFailed,
    MessageSnapshot,
    TurnSettled,
)
from lyra.domain.analytics.types import AccountScope

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)


class FakeStore:
    """内存仓储，记录调用顺序以便验证顺序约束。"""

    def __init__(self, *, reject_save: bool = False) -> None:
        self.calls: list[str] = []
        self.messages: list[Any] = []
        self.saved: list[AgentSessionSnapshot] = []
        self.reject_save = reject_save

    def save_session(self, session: AgentSessionSnapshot, *, next_event_sequence: int) -> bool:
        if self.reject_save:
            return False
        self.calls.append("save")
        self.saved.append(session)
        return True

    def append_message(self, message: Any) -> None:
        self.calls.append("message")
        self.messages.append(message)


class FakeExecutor:
    """按脚本产生流信号的执行器。"""

    def __init__(self, signals: list[Any]) -> None:
        self._signals = signals
        self.calls: list[dict[str, Any]] = []

    def run(self, *, session, message_id: str, content: str):
        # ``session`` 会被传入：系统提示词需要账户范围与账本版本。
        self.calls.append({
            "session_id": session.id, "message_id": message_id, "content": content
        })
        return list(self._signals)


def _session(status: str = "idle") -> AgentSessionSnapshot:
    return AgentSessionSnapshot(
        id="s-1", provider="genkit", status=status,  # type: ignore[arg-type]
        account_scope=AccountScope(kind="all"), ledger_version=3, as_of="t",
        revision=1, summary=None, messages=(),
    )


def _clock():
    """每次调用前进 1 秒，让时间戳可区分。"""
    state = {"now": NOW}

    def now() -> datetime:
        state["now"] = state["now"] + timedelta(seconds=1)
        return state["now"]

    return now


def _orchestrator(*, store=None, on_event=None, session=None):
    orchestrator = AgentTurnOrchestrator(
        store=store or FakeStore(), now=_clock(), on_event=on_event
    )
    orchestrator.register(session or _session())
    return orchestrator


SUCCESS = [
    MessageDelta(delta="本月净收益 "),
    MessageDelta(delta="200"),
    MessageSnapshot(content="本月净收益 200", stop_reason="stop"),
    TurnSettled(),
]


# --------------------------------------------------------------------------
# 成功回合
# --------------------------------------------------------------------------


def test_successful_turn_returns_to_idle() -> None:
    """成功回合：会话回到 idle，助手消息落库。"""
    store = FakeStore()
    result = _orchestrator(store=store).run_turn(
        session_id="s-1", executor=FakeExecutor(SUCCESS), content="这个月赚了多少"
    )

    assert result.snapshot.status == "idle"
    # 用户消息 + 助手消息
    assert len(store.messages) == 2
    assert store.messages[0].role == "user"
    assert store.messages[1].role == "assistant"
    assert store.messages[1].content == "本月净收益 200"


def test_event_sequences_are_contiguous() -> None:
    """事件序号从 1 开始且严格连续。"""
    result = _orchestrator().run_turn(
        session_id="s-1", executor=FakeExecutor(SUCCESS), content="x"
    )

    sequences = [event.sequence for event in result.events]
    assert sequences == list(range(1, len(sequences) + 1))


def test_events_include_start_deltas_usage_and_completion() -> None:
    """事件类型覆盖开始、增量与完成。"""
    result = _orchestrator().run_turn(
        session_id="s-1", executor=FakeExecutor(SUCCESS), content="x"
    )

    types = [event.type for event in result.events]
    assert types[0] == "message.started"
    assert types.count("message.delta") == 2
    assert types[-1] == "message.completed"


def test_persist_happens_before_execution() -> None:
    """**先持久化再执行**：仓储的 save 必须早于执行器的 run。"""
    store = FakeStore()
    orchestrator = _orchestrator(store=store)
    order: list[str] = []

    class TracingExecutor(FakeExecutor):
        def run(self, *, session, message_id: str, content: str):
            order.append("execute")
            return super().run(session=session, message_id=message_id, content=content)

    original_save = store.save_session

    def tracing_save(session, *, next_event_sequence):
        order.append("persist")
        return original_save(session, next_event_sequence=next_event_sequence)

    store.save_session = tracing_save  # type: ignore[method-assign]

    orchestrator.run_turn(session_id="s-1", executor=TracingExecutor(SUCCESS), content="x")

    assert order[0] == "persist", "先启动了模型才落库"


def test_events_are_pushed_after_persistence() -> None:
    """**事件在持久化之后才推送**。"""
    order: list[str] = []
    store = FakeStore()
    original_save = store.save_session

    def tracing_save(session, *, next_event_sequence):
        order.append("persist")
        return original_save(session, next_event_sequence=next_event_sequence)

    store.save_session = tracing_save  # type: ignore[method-assign]
    orchestrator = _orchestrator(store=store, on_event=lambda event: order.append("push"))

    orchestrator.run_turn(session_id="s-1", executor=FakeExecutor(SUCCESS), content="x")

    # 第一个事件推送前必须已经落库。
    assert order.index("persist") < order.index("push")


# --------------------------------------------------------------------------
# 失败与未收口
# --------------------------------------------------------------------------


def test_failure_outcome_finalizes_as_failed() -> None:
    """执行器上报失败 → 会话回到 idle，事件为 message.failed。"""
    result = _orchestrator().run_turn(
        session_id="s-1", executor=FakeExecutor([MessageFailed(outcome="failed")]),
        content="x",
    )

    assert result.snapshot.status == "idle"
    assert result.events[-1].type == "message.failed"
    assert result.events[-1].payload["error_code"] == TURN_FAILED_CODE


def test_executor_without_settlement_but_with_snapshot_succeeds() -> None:
    """执行器未收口但已给出权威快照 → 仍按成功收口。

    领域层明确拒绝"成功快照后改写为失败"，因此编排器**不**强行改成失败——
    强行改成失败等于用一个更糟的错误掩盖执行器的问题。
    """
    result = _orchestrator().run_turn(
        session_id="s-1",
        executor=FakeExecutor([MessageSnapshot(content="答案", stop_reason="stop")]),
        content="x",
    )

    assert result.snapshot.status == "idle"
    assert result.events[-1].type == "message.completed"


def test_executor_without_snapshot_or_settlement_fails() -> None:
    """既没收口也没有权威快照 → 失败（不能把"没拿到回答"当成成功）。"""
    result = _orchestrator().run_turn(
        session_id="s-1",
        executor=FakeExecutor([MessageDelta(delta="半截")]),
        content="x",
    )

    assert result.snapshot.status == "idle"
    assert result.events[-1].type == "message.failed"
    assert result.events[-1].payload["outcome"] == "failed"


def test_partial_text_is_kept_as_a_failed_message() -> None:
    """未收口但已产生可见文本 → 保留为明确标记的失败消息。"""
    store = FakeStore()
    _orchestrator(store=store).run_turn(
        session_id="s-1",
        executor=FakeExecutor([MessageDelta(delta="半截回答")]),
        content="x",
    )

    assistants = [message for message in store.messages if message.role == "assistant"]
    assert len(assistants) == 1
    assert assistants[0].status == "failed"
    assert assistants[0].content == "半截回答"


# --------------------------------------------------------------------------
# 并发与续传
# --------------------------------------------------------------------------


def test_concurrent_modification_is_rejected() -> None:
    """CAS 失败必须报错，而不是默默覆盖别人的修改。"""
    orchestrator = _orchestrator(store=FakeStore(reject_save=True))

    with pytest.raises(ValueError, match="并发修改"):
        orchestrator.run_turn(session_id="s-1", executor=FakeExecutor(SUCCESS), content="x")


def test_unregistered_session_is_rejected() -> None:
    """未登记的会话不能执行回合。"""
    with pytest.raises(KeyError, match="未登记"):
        _orchestrator().run_turn(
            session_id="nope", executor=FakeExecutor(SUCCESS), content="x"
        )


def test_resume_returns_buffered_events() -> None:
    """回合结束后可按游标续传。"""
    orchestrator = _orchestrator()
    result = orchestrator.run_turn(
        session_id="s-1", executor=FakeExecutor(SUCCESS), content="x"
    )

    plan = orchestrator.resume_events("s-1", after_sequence=1)

    assert plan.mode == "events"
    assert plan.events[0].sequence == 2
    assert len(plan.events) == len(result.events) - 1


def test_runtime_exposes_the_latest_snapshot() -> None:
    """登记后可从编排器取到会话快照。"""
    orchestrator = _orchestrator()
    assert orchestrator.runtime("s-1") is not None
    assert orchestrator.runtime("nope") is None


def test_second_turn_starts_from_idle() -> None:
    """回合结束后可以再开一回合。"""
    orchestrator = _orchestrator()

    first = orchestrator.run_turn(
        session_id="s-1", executor=FakeExecutor(SUCCESS), content="第一问"
    )
    second = orchestrator.run_turn(
        session_id="s-1", executor=FakeExecutor(SUCCESS), content="第二问"
    )

    assert first.snapshot.status == "idle"
    assert second.snapshot.status == "idle"
    # 第二回合的事件序号接着第一回合继续。
    assert second.events[0].sequence == first.events[-1].sequence + 1
