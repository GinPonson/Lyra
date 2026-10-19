"""Agent 回合计划测试。

两条刻意的约束：

1. **成功回合的助手内容必须非空**。空内容走失败或取消路径；用"空成功消息"收口
   会让客户端显示一个没有任何内容的回答，且看起来是正常的；
2. **失败/取消时没有可见文本就不生成消息**。凭空造一条空助手消息，会话历史里
   会出现"模型说过话但什么都没说"的记录。
"""

from __future__ import annotations

import pytest

from lyra.domain.agent.lifecycle import AgentSessionStateError
from lyra.domain.agent.switching import (
    AgentSessionSnapshot,
    NormalizedAgentMessage,
)
from lyra.domain.agent.turn import (
    TURN_CANCELLED_CODE,
    TURN_FAILED_CODE,
    complete_agent_turn,
    fail_agent_turn,
    start_agent_turn,
)
from lyra.domain.analytics.types import AccountScope


def _session(status: str = "idle", messages: tuple = (), scope=None) -> AgentSessionSnapshot:
    return AgentSessionSnapshot(
        id="s-1", provider="genkit", status=status,  # type: ignore[arg-type]
        account_scope=scope or AccountScope(kind="all"), ledger_version=3,
        as_of="t", revision=1, summary="旧摘要", messages=messages,
    )


# --------------------------------------------------------------------------
# 开始
# --------------------------------------------------------------------------


def test_start_turn_moves_to_running_and_appends_user_message() -> None:
    """开始回合：转 running、追加用户消息、发 message.started。"""
    plan = start_agent_turn(
        session=_session(), message_id="m-1", content="这个月赚了多少",
        started_at="2026-10-12T09:00:00.000Z", last_event_sequence=0,
    )

    assert plan.session.status == "running"
    assert plan.session.revision == 2
    assert plan.user_message.role == "user"
    assert plan.session.messages[-1].id == "m-1"
    assert plan.event.type == "message.started"
    assert plan.event.sequence == 1
    assert plan.adapter_input == {"messageId": "m-1", "content": "这个月赚了多少"}


def test_start_turn_event_carries_the_baseline() -> None:
    """事件必须带上数据基线，否则客户端无法判断答案适用于哪个版本。"""
    plan = start_agent_turn(
        session=_session(scope=AccountScope(kind="account", account="TST001")),
        message_id="m-1", content="x", started_at="t", last_event_sequence=5,
    )

    assert plan.event.payload["ledger_version"] == 3
    assert plan.event.payload["account_scope"] == {"kind": "account", "account": "TST001"}
    assert plan.event.sequence == 6


def test_start_turn_requires_idle() -> None:
    """非 idle 不能开始回合。"""
    with pytest.raises(AgentSessionStateError):
        start_agent_turn(
            session=_session("running"), message_id="m", content="x",
            started_at="t", last_event_sequence=0,
        )


def test_start_turn_rejects_blank_content() -> None:
    """空内容必须拒绝。"""
    with pytest.raises(ValueError, match="不能为空"):
        start_agent_turn(
            session=_session(), message_id="m", content="   ",
            started_at="t", last_event_sequence=0,
        )


def test_start_turn_rejects_duplicate_message_id() -> None:
    """消息 ID 不得与历史重复。"""
    existing = NormalizedAgentMessage(id="m-1", role="user", content="x", created_at="t")
    with pytest.raises(ValueError, match="已存在"):
        start_agent_turn(
            session=_session(messages=(existing,)), message_id="m-1", content="y",
            started_at="t", last_event_sequence=0,
        )


# --------------------------------------------------------------------------
# 完成
# --------------------------------------------------------------------------


def test_complete_turn_returns_to_idle_with_assistant_message() -> None:
    """完成回合：转回 idle、追加助手消息、发 message.completed。"""
    plan = complete_agent_turn(
        session=_session("running"), message_id="a-1", content="本月净收益 200",
        completed_at="2026-10-12T09:00:05.000Z", last_event_sequence=1,
    )

    assert plan.session.status == "idle"
    assert plan.assistant_message.role == "assistant"
    assert plan.assistant_message.status == "completed"
    assert plan.assistant_message.error_code is None
    assert plan.event.type == "message.completed"
    assert plan.event.sequence == 2


def test_complete_turn_requires_non_empty_content() -> None:
    """**成功回合必须有内容**；空内容应走失败或取消路径。"""
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="Agent 助手消息不能为空"):
            complete_agent_turn(
                session=_session("running"), message_id="a", content=blank,
                completed_at="t", last_event_sequence=0,
            )


def test_complete_turn_requires_running() -> None:
    """只有 running 会话可以成功收口。"""
    for status in ("idle", "cancelling", "closed"):
        with pytest.raises(AgentSessionStateError):
            complete_agent_turn(
                session=_session(status), message_id="a", content="x",
                completed_at="t", last_event_sequence=0,
            )


def test_complete_turn_clears_the_summary() -> None:
    """完成回合后摘要保持不变——清理由基线/范围变更负责。"""
    plan = complete_agent_turn(
        session=_session("running"), message_id="a", content="x",
        completed_at="t", last_event_sequence=0,
    )
    assert plan.session.summary == "旧摘要"


# --------------------------------------------------------------------------
# 失败与取消
# --------------------------------------------------------------------------


def test_failed_outcome_requires_running() -> None:
    """failed 只收口 running。"""
    plan = fail_agent_turn(
        session=_session("running"), message_id="a-1", outcome="failed",
        partial_content=None, finished_at="t", last_event_sequence=0,
    )
    assert plan.session.status == "idle"
    assert plan.event.payload["error_code"] == TURN_FAILED_CODE


def test_cancelled_outcome_requires_cancelling() -> None:
    """cancelled 只收口 cancelling——取消必须经过那个中间态。"""
    plan = fail_agent_turn(
        session=_session("cancelling"), message_id="a-1", outcome="cancelled",
        partial_content=None, finished_at="t", last_event_sequence=0,
    )
    assert plan.event.payload["error_code"] == TURN_CANCELLED_CODE

    with pytest.raises(AgentSessionStateError):
        fail_agent_turn(
            session=_session("running"), message_id="a-1", outcome="cancelled",
            partial_content=None, finished_at="t", last_event_sequence=0,
        )


def test_empty_partial_content_produces_no_message() -> None:
    """**没有可见文本就不生成消息**，避免历史里出现空的助手消息。"""
    for partial in (None, "", "   "):
        plan = fail_agent_turn(
            session=_session("running"), message_id="a-1", outcome="failed",
            partial_content=partial, finished_at="t", last_event_sequence=0,
        )
        assert plan.assistant_message is None
        assert plan.session.messages == ()
        # 事件仍然发出，客户端据此收口。
        assert plan.event.message_id == "a-1"


def test_partial_content_becomes_a_failed_message() -> None:
    """有可见文本时保留为明确标记的助手消息。"""
    plan = fail_agent_turn(
        session=_session("running"), message_id="a-1", outcome="failed",
        partial_content="  本月净收益 2", finished_at="t", last_event_sequence=0,
    )

    assert plan.assistant_message is not None
    assert plan.assistant_message.content == "本月净收益 2"
    assert plan.assistant_message.status == "failed"
    assert plan.assistant_message.error_code == TURN_FAILED_CODE


def test_failure_event_does_not_copy_the_content() -> None:
    """事件只带稳定错误码与基线，**不复制正文或底层供应商错误**。"""
    plan = fail_agent_turn(
        session=_session("running"), message_id="a-1", outcome="failed",
        partial_content="秘密内容", finished_at="t", last_event_sequence=0,
    )

    assert "秘密内容" not in str(plan.event.payload)
    assert set(plan.event.payload) == {
        "provider", "ledger_version", "outcome", "error_code"
    }


def test_failure_rejects_duplicate_message_id() -> None:
    """失败消息 ID 也不得与历史重复。"""
    existing = NormalizedAgentMessage(id="a-1", role="assistant", content="x", created_at="t")
    with pytest.raises(ValueError, match="已存在"):
        fail_agent_turn(
            session=_session("running", messages=(existing,)), message_id="a-1",
            outcome="failed", partial_content="y", finished_at="t", last_event_sequence=0,
        )


def test_original_session_is_not_mutated() -> None:
    """三个计划函数都返回新快照。"""
    session = _session("running")
    complete_agent_turn(
        session=session, message_id="a", content="x", completed_at="t",
        last_event_sequence=0,
    )
    assert session.status == "running"
    assert session.messages == ()
