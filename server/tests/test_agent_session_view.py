"""Agent 会话公开投影测试。

两条要点：

1. **逐字段重建**而非透传对象——内部运行状态与供应商私有字段不会漏进响应；
2. **「未设置」与「显式为空」不同**。TypeScript 用 ``undefined`` / ``null`` 区分，
   公开投影对前者省略键、对后者保留 ``null``。Python 的 ``None`` 只能表达一种，
   因此用哨兵区分。混同会让客户端看到 `{"errorCode": null}` 与没有该键两种情况
   被当成同一回事。
"""

from __future__ import annotations

from lyra.application.public_projection import project_agent_session
from lyra.domain.agent.switching import (
    UNSET,
    AgentProviderAvailability,
    AgentSessionSnapshot,
    NormalizedAgentMessage,
    create_agent_session,
)
from lyra.domain.analytics.types import AccountScope


def _snapshot(*messages: NormalizedAgentMessage, scope: AccountScope | None = None):
    return AgentSessionSnapshot(
        id="session-1", provider="genkit", status="idle",
        account_scope=scope or AccountScope(kind="all"),
        ledger_version=3, as_of="2026-10-12T09:00:00.000Z", revision=2,
        summary="内部摘要，不应出现在公开响应里", messages=messages,
    )


def test_projects_session_fields_as_camel_case() -> None:
    """会话字段为 camelCase，且账户范围逐字段重建。"""
    public = project_agent_session(_snapshot(scope=AccountScope(kind="account", account="TST001")))

    assert public["id"] == "session-1"
    assert public["provider"] == "genkit"
    assert public["status"] == "idle"
    assert public["ledgerVersion"] == 3
    assert public["asOf"] == "2026-10-12T09:00:00.000Z"
    assert public["revision"] == 2
    assert public["accountScope"] == {"kind": "account", "account": "TST001"}
    assert set(public) == {
        "id", "provider", "status", "accountScope", "ledgerVersion", "asOf",
        "revision", "messages",
    }


def test_summary_is_not_exposed() -> None:
    """内部摘要不得出现在公开会话里。"""
    public = project_agent_session(_snapshot())
    assert "summary" not in public
    assert "内部摘要" not in str(public)


def test_all_scope_projects_to_kind_only() -> None:
    """``all`` 范围不带 account 字段。"""
    public = project_agent_session(_snapshot(scope=AccountScope(kind="all")))
    assert public["accountScope"] == {"kind": "all"}


def test_unset_optional_fields_are_omitted() -> None:
    """未设置的可选字段**不出现**在响应里。"""
    message = NormalizedAgentMessage(
        id="m-1", role="user", content="你好", created_at="t"
    )
    public = project_agent_session(_snapshot(message))

    assert public["messages"][0] == {
        "id": "m-1", "role": "user", "content": "你好", "createdAt": "t",
    }
    assert "status" not in public["messages"][0]
    assert "errorCode" not in public["messages"][0]


def test_explicit_null_error_code_is_preserved() -> None:
    """**显式为空必须保留 `null`**，而不是与「未设置」混同。"""
    message = NormalizedAgentMessage(
        id="m-1", role="assistant", content="答案", created_at="t",
        status="completed", error_code=None,
    )
    public = project_agent_session(_snapshot(message))

    assert public["messages"][0]["status"] == "completed"
    assert "errorCode" in public["messages"][0]
    assert public["messages"][0]["errorCode"] is None


def test_present_error_code_is_preserved() -> None:
    """有错误码时原样保留。"""
    message = NormalizedAgentMessage(
        id="m-1", role="assistant", content="", created_at="t",
        status="failed", error_code="TOOL_EXECUTION_FAILED",
    )
    public = project_agent_session(_snapshot(message))

    assert public["messages"][0]["errorCode"] == "TOOL_EXECUTION_FAILED"
    assert public["messages"][0]["status"] == "failed"


def test_sentinel_default_is_unset_not_none() -> None:
    """默认值必须是哨兵，而不是 ``None``——否则无法区分两种语义。"""
    message = NormalizedAgentMessage(id="m", role="user", content="", created_at="t")
    assert message.error_code is UNSET
    assert message.error_code is not None


def test_projection_works_on_a_session_created_by_the_domain() -> None:
    """领域层创建的会话可直接投影。"""
    session = create_agent_session(
        session_id="s-1", provider="genkit",
        availability=AgentProviderAvailability(provider="genkit", status="available"),
        account_scope=AccountScope(kind="all"), ledger_version=0, as_of="t",
    )
    public = project_agent_session(session)

    assert public["messages"] == []
    assert public["revision"] == 1
