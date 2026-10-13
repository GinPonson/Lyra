"""Agent 会话状态与回合边界切换测试。

最要紧的两条：

1. **切换只能在完整回合边界**（``idle``）。半途换范围会让同一段对话前后数字口径
   不同，而模型已经引用过旧数字；
2. **切换与刷新都必须作废旧摘要**。摘要可能含原账户或旧基线的具体数字；若保留，
   下一回合模型会拿旧数字回答新范围的问题——**而且答案看起来完全正常**。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from lyra.domain.agent.switching import (
    AgentProviderAvailability,
    AgentSessionSnapshot,
    AgentSwitchError,
    NormalizedAgentMessage,
    create_agent_session,
    prepare_agent_account_scope_change,
    prepare_agent_ledger_baseline_refresh,
)
from lyra.domain.analytics.types import AccountScope

ALL = AccountScope(kind="all")
ONE = AccountScope(kind="account", account="TST001")
OTHER = AccountScope(kind="account", account="TST002")


def _available(provider: str = "genkit") -> AgentProviderAvailability:
    return AgentProviderAvailability(provider=provider, status="available")  # type: ignore[arg-type]


def _session(status: str = "idle", scope: AccountScope = ALL, version: int = 3):
    """构造一个会话；summary 预置内容以便验证"必须作废"。"""
    return AgentSessionSnapshot(
        id="session-1", provider="genkit", status=status,  # type: ignore[arg-type]
        account_scope=scope, ledger_version=version, as_of="2026-10-12T09:00:00.000Z",
        revision=1, summary="此前回合摘要，含账户 TST001 的具体数字", messages=(),
    )


# --------------------------------------------------------------------------
# 创建
# --------------------------------------------------------------------------


def test_creates_idle_session_at_first_baseline() -> None:
    """创建后是 idle、revision 1、无摘要与消息。"""
    session = create_agent_session(
        session_id="session-1", provider="genkit", availability=_available(),
        account_scope=ONE, ledger_version=3, as_of="2026-10-12T09:00:00.000Z",
    )

    assert session.status == "idle"
    assert session.revision == 1
    assert session.summary is None
    assert session.messages == ()
    assert session.account_scope == ONE


def test_create_rejects_unavailable_provider_without_leaking_reason() -> None:
    """不可用时给稳定错误码，原因只作为内部字段，传输层不得返回。"""
    unavailable = AgentProviderAvailability(
        provider="genkit", status="unavailable", reason="binary not found at /usr/local/bin/x"
    )

    with pytest.raises(AgentSwitchError) as raised:
        create_agent_session(
            session_id="s", provider="genkit", availability=unavailable,
            account_scope=ALL, ledger_version=0, as_of="2026-10-12T09:00:00.000Z",
        )

    assert raised.value.code == "AGENT_UNAVAILABLE"
    # 安全说明里不得出现路径。
    assert "/usr/local/bin" not in raised.value.message
    # 内部原因保留供服务端日志使用。
    assert raised.value.internal_reason is not None


def test_create_rejects_provider_mismatch() -> None:
    """可用性结果与会话供应商不一致说明调用方出错。"""
    with pytest.raises(ValueError, match="不一致"):
        create_agent_session(
            session_id="s", provider="genkit",
            availability=AgentProviderAvailability(provider="other", status="available"),  # type: ignore[arg-type]
            account_scope=ALL, ledger_version=0, as_of="t",
        )


@pytest.mark.parametrize("version", [-1, 1.5, True])
def test_create_rejects_invalid_ledger_version(version: object) -> None:
    """初始账本版本必须是非负整数。"""
    with pytest.raises(ValueError, match="非负安全整数"):
        create_agent_session(
            session_id="s", provider="genkit", availability=_available(),
            account_scope=ALL, ledger_version=version, as_of="t",  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# 账户范围切换
# --------------------------------------------------------------------------


def test_scope_change_clears_summary_and_appends_system_message() -> None:
    """切换范围必须作废旧摘要，并追加划界系统消息。"""
    plan = prepare_agent_account_scope_change(
        session=_session(), account_scope=OTHER, ledger_version=5,
        as_of="2026-10-12T10:00:00.000Z", message_id="msg-1",
        changed_at="2026-10-12T10:00:00.000Z",
    )

    assert plan.next_session.account_scope == OTHER
    assert plan.next_session.ledger_version == 5
    assert plan.next_session.revision == 2
    assert plan.next_session.summary is None, "切换范围却保留了旧摘要"
    assert plan.next_session.messages[-1].role == "system"
    assert plan.context_message.id == "msg-1"


def test_scope_change_message_states_both_scopes() -> None:
    """划界消息必须写明前后范围与基线，便于人工复核。"""
    plan = prepare_agent_account_scope_change(
        session=_session(), account_scope=OTHER, ledger_version=5,
        as_of="2026-10-12T10:00:00.000Z", message_id="m", changed_at="t",
    )

    assert "全部活跃账户" in plan.context_message.content
    assert "单账户 TST002" in plan.context_message.content
    assert "5" in plan.context_message.content
    assert "TST002" in plan.context_message.content


@pytest.mark.parametrize("status", ["running", "cancelling", "closed"])
def test_scope_change_requires_turn_boundary(status: str) -> None:
    """非 idle 状态一律拒绝，且消息里带上当前状态便于排查。"""
    with pytest.raises(AgentSwitchError) as raised:
        prepare_agent_account_scope_change(
            session=_session(status=status), account_scope=OTHER, ledger_version=5,
            as_of="t", message_id="m", changed_at="t",
        )

    assert raised.value.code == "AGENT_SWITCH_REQUIRES_TURN_BOUNDARY"
    assert status in raised.value.message


def test_scope_change_rejects_unchanged_scope() -> None:
    """目标范围与当前相同是调用方 bug。"""
    with pytest.raises(ValueError, match="已经使用目标账户范围"):
        prepare_agent_account_scope_change(
            session=_session(scope=ONE), account_scope=ONE, ledger_version=5,
            as_of="t", message_id="m", changed_at="t",
        )


def test_scope_change_rejects_duplicate_message_id() -> None:
    """划界消息 ID 重复会让历史里出现两条同 ID 消息。"""
    existing = NormalizedAgentMessage(id="m", role="system", content="x", created_at="t")
    session = replace(_session(), messages=(existing,))

    with pytest.raises(ValueError, match="已存在"):
        prepare_agent_account_scope_change(
            session=session, account_scope=OTHER, ledger_version=5,
            as_of="t", message_id="m", changed_at="t",
        )


# --------------------------------------------------------------------------
# 基线刷新
# --------------------------------------------------------------------------


def test_baseline_refresh_clears_summary_and_keeps_scope() -> None:
    """刷新基线必须作废旧摘要，账户范围保持不变。"""
    plan = prepare_agent_ledger_baseline_refresh(
        session=_session(version=3), ledger_version=4,
        as_of="2026-10-12T11:00:00.000Z", message_id="m", refreshed_at="t",
    )

    assert plan.next_session.account_scope == ALL
    assert plan.next_session.ledger_version == 4
    assert plan.next_session.revision == 2
    assert plan.next_session.summary is None, "刷新基线却保留了旧摘要"
    assert "3" in plan.context_message.content and "4" in plan.context_message.content


@pytest.mark.parametrize("version", [3, 2, 0])
def test_baseline_refresh_requires_strictly_increasing_version(version: int) -> None:
    """只能刷新到**更高**的版本——同版本或回退都会让新基线名不副实。"""
    with pytest.raises(ValueError, match="只能刷新到更高的账本版本"):
        prepare_agent_ledger_baseline_refresh(
            session=_session(version=3), ledger_version=version,
            as_of="t", message_id="m", refreshed_at="t",
        )


@pytest.mark.parametrize("status", ["running", "cancelling", "closed"])
def test_baseline_refresh_requires_turn_boundary(status: str) -> None:
    """非 idle 状态一律拒绝。"""
    with pytest.raises(AgentSwitchError) as raised:
        prepare_agent_ledger_baseline_refresh(
            session=_session(status=status, version=3), ledger_version=4,
            as_of="t", message_id="m", refreshed_at="t",
        )
    assert raised.value.code == "AGENT_SWITCH_REQUIRES_TURN_BOUNDARY"


def test_original_session_is_not_mutated() -> None:
    """两个准备函数都返回新快照，不改动传入的会话。"""
    session = _session()
    before = (session.summary, session.ledger_version, session.revision)

    prepare_agent_ledger_baseline_refresh(
        session=session, ledger_version=9, as_of="t", message_id="m", refreshed_at="t"
    )

    assert (session.summary, session.ledger_version, session.revision) == before
