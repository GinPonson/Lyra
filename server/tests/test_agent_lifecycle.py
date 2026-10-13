"""Agent 会话状态转换测试。

两条性质：

1. **同状态重放返回原对象**。`revision` 是客户端判断"会话是否变过"的依据；
   一次无变化的轮询若也递增它，客户端会以为状态变了；
2. **`running` 回到 `idle` 必须经 `cancelling` 之外只有完成后直回**——取消有一个
   可观测的中间态，而不是从"正在生成"直接跳到"已取消"（那会让客户端无法区分
   "完成后正常结束"与"被取消"）。
"""

from __future__ import annotations

import pytest

from lyra.domain.agent.lifecycle import (
    ALLOWED_TRANSITIONS,
    AgentSessionStateError,
    transition_agent_session,
)
from lyra.domain.agent.switching import AgentSessionSnapshot
from lyra.domain.analytics.types import AccountScope

STATUSES = ("idle", "running", "cancelling", "closed")


def _session(status: str = "idle") -> AgentSessionSnapshot:
    return AgentSessionSnapshot(
        id="s-1", provider="genkit", status=status,  # type: ignore[arg-type]
        account_scope=AccountScope(kind="all"), ledger_version=0,
        as_of="t", revision=1, summary=None, messages=(),
    )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("idle", "running"), ("idle", "closed"),
        ("running", "cancelling"), ("running", "idle"),
        ("cancelling", "idle"),
    ],
)
def test_allowed_transitions_bump_revision(current: str, target: str) -> None:
    """允许的转换都递增 revision。"""
    result = transition_agent_session(_session(current), target)  # type: ignore[arg-type]

    assert result.status == target
    assert result.revision == 2
    assert result.id == "s-1"


@pytest.mark.parametrize("status", STATUSES)
def test_same_status_returns_the_same_object(status: str) -> None:
    """**同状态重放返回原对象**，不递增 revision。"""
    session = _session(status)
    assert transition_agent_session(session, status) is session  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("idle", "cancelling"),   # 未开始就取消
        ("cancelling", "closed"),  # 取消中不能直接关闭
        ("closed", "idle"),        # 终态无出边
        ("closed", "running"),
        ("running", "closed"),     # 运行中不能直接关闭
    ],
)
def test_illegal_transitions_are_rejected(current: str, target: str) -> None:
    """非法转换必须报错，且错误里带上前后状态。"""
    with pytest.raises(AgentSessionStateError) as raised:
        transition_agent_session(_session(current), target)  # type: ignore[arg-type]

    assert raised.value.code == "AGENT_SESSION_STATE_CONFLICT"
    assert current in str(raised.value)
    assert target in str(raised.value)


def test_closed_has_no_outgoing_edges() -> None:
    """closed 是终态。"""
    assert ALLOWED_TRANSITIONS["closed"] == ()


def test_every_status_has_an_entry() -> None:
    """每个状态都必须在转换表里有条目，否则查表会 KeyError。"""
    for status in STATUSES:
        assert status in ALLOWED_TRANSITIONS


def test_unknown_status_is_rejected() -> None:
    """未知状态是调用方 bug。"""
    with pytest.raises(ValueError, match="未知的 Agent 会话状态"):
        transition_agent_session(_session(), "nonsense")  # type: ignore[arg-type]


def test_original_session_is_not_mutated() -> None:
    """返回新快照，不改动传入对象。"""
    session = _session("idle")
    transition_agent_session(session, "running")
    assert session.status == "idle"
    assert session.revision == 1


def test_cancel_path_is_observable() -> None:
    """取消有一条可观测路径：running → cancelling → idle。"""
    running = transition_agent_session(_session("idle"), "running")
    cancelling = transition_agent_session(running, "cancelling")
    back = transition_agent_session(cancelling, "idle")

    assert [running.status, cancelling.status, back.status] == [
        "running", "cancelling", "idle"
    ]
    assert [running.revision, cancelling.revision, back.revision] == [2, 3, 4]
