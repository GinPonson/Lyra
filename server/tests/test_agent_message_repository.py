"""消息与工具调用仓储测试（真实 DuckDB）。

两条并发性性质的验证：

1. **完成更新只作用于 `streaming` / `started` 行** —— 迟到的完成回调不得覆盖
   已终止消息的完成时间与错误码；
2. **`last_event_sequence` 用 CAS 递增** —— 用 `+1` 直接写会让两个并发回合产生
   相同序号，客户端看到的事件顺序就不确定。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from lyra.db.connection import open_lyra_database
from lyra.domain.agent.messages import (
    finish_agent_message,
    finish_tool_call,
    reject_tool_call,
    start_agent_message,
    start_tool_call,
)
from lyra.domain.agent.switching import AgentProviderAvailability, create_agent_session
from lyra.domain.analytics.types import AccountScope
from lyra.infrastructure.db.agent_message_repository import AgentMessageRepository
from lyra.infrastructure.db.agent_session_repository import DuckDbAgentSessionRepository

ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"
DEVICE_ID = "22222222-2222-2222-2222-222222222222"
SESSION_ID = "33333333-3333-3333-3333-333333333333"
NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
T1 = "2026-10-12T09:00:01.000Z"
T2 = "2026-10-12T09:00:02.000Z"


@pytest.fixture()
def wired(tmp_path: Path):
    """装配真实库、账户、设备与会话。"""
    database = open_lyra_database(str(tmp_path / "msg.duckdb"))
    connection = database.connection
    connection.execute(
        """
        INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), 'Account', 'TST001', '001')
        """,
        [ACCOUNT_ID],
    )
    connection.execute(
        """
        INSERT INTO paired_devices
            (id, display_name, public_key_pem, public_key_fingerprint, key_algorithm, status)
        VALUES (CAST(? AS UUID), 'Pixel 9', '-----BEGIN PUBLIC KEY-----', ?, 'ec', 'active')
        """,
        [DEVICE_ID, "f" * 64],
    )
    DuckDbAgentSessionRepository(connection).create_session(
        create_agent_session(
            session_id=SESSION_ID, provider="genkit",
            availability=AgentProviderAvailability(provider="genkit", status="available"),
            account_scope=AccountScope(kind="all"), ledger_version=0, as_of=T1,
        ),
        device_id=DEVICE_ID, now=NOW,
    )
    try:
        yield AgentMessageRepository(connection), connection
    finally:
        database.close()


def _message(ordinal: int = 1, message_id: str = "44444444-4444-4444-4444-444444444444"):
    return start_agent_message(
        message_id=message_id, session_id=SESSION_ID, ordinal=ordinal,
        role="assistant", content="", created_at=T1,
    )


def _call(call_id: str = "55555555-5555-5555-5555-555555555555", ordinal: int = 1):
    return start_tool_call(
        call_id=call_id, session_id=SESSION_ID,
        message_id="44444444-4444-4444-4444-444444444444", ordinal=ordinal,
        tool_name="summary",
        arguments_summary={"account": "TST001"}, ledger_version=0, started_at=T1,
    )


# --------------------------------------------------------------------------
# 消息
# --------------------------------------------------------------------------


def test_append_and_load_message(wired) -> None:
    """写入后按序号读回，时间戳为 ISO 文本。"""
    repository, _ = wired
    repository.append_message(_message())

    messages = repository.load_messages(SESSION_ID)

    assert len(messages) == 1
    assert messages[0].status == "streaming"
    assert messages[0].role == "assistant"
    assert messages[0].created_at == T1
    assert messages[0].completed_at is None


def test_messages_are_ordered_by_ordinal(wired) -> None:
    """读取必须按序号升序，而不是插入顺序。"""
    repository, _ = wired
    repository.append_message(_message(ordinal=2, message_id="66666666-6666-6666-6666-666666666666"))
    repository.append_message(_message(ordinal=1))

    messages = repository.load_messages(SESSION_ID)

    assert [message.ordinal for message in messages] == [1, 2]


def test_duplicate_ordinal_is_rejected_by_the_database(wired) -> None:
    """序号唯一约束是兜底：编排层算错时会在这里失败，而不是静默写重号。"""
    repository, _ = wired
    repository.append_message(_message(ordinal=1))

    with pytest.raises(Exception, match="constraint|Constraint"):
        repository.append_message(
            _message(ordinal=1, message_id="77777777-7777-7777-7777-777777777777")
        )


def test_complete_message_transitions_to_terminal(wired) -> None:
    """完成消息写入终态、内容与完成时间。"""
    repository, _ = wired
    repository.append_message(_message())

    finished = finish_agent_message(
        _message(), status="completed", content="答案", completed_at=T2
    )
    assert repository.complete_message(finished) is True

    stored = repository.load_messages(SESSION_ID)[0]
    assert stored.status == "completed"
    assert stored.content == "答案"
    assert stored.completed_at == T2


def test_late_completion_cannot_overwrite_a_terminal_message(wired) -> None:
    """**迟到的完成回调不得覆盖已终止消息。**

    只在仍处于 ``streaming`` 时才更新；否则完成时间会被改写，审计信息失真。
    """
    repository, _ = wired
    repository.append_message(_message())
    repository.complete_message(
        finish_agent_message(_message(), status="completed", content="初稿", completed_at=T1)
    )

    late = finish_agent_message(_message(), status="failed", completed_at=T2)
    assert repository.complete_message(late) is False

    stored = repository.load_messages(SESSION_ID)[0]
    assert stored.status == "completed"
    assert stored.completed_at == T1


# --------------------------------------------------------------------------
# 工具调用
# --------------------------------------------------------------------------


def test_append_and_complete_tool_call(wired) -> None:
    """工具调用写入后可按 CAS 更新为终态。"""
    repository, connection = wired
    # agent_tool_calls.message_id 有指向 agent_messages 的外键，先写消息。
    repository.append_message(_message())
    repository.append_tool_call(_call())

    stored = connection.execute(
        "SELECT status, tool_name, authorization_decision, arguments_summary FROM agent_tool_calls"
    ).fetchone()
    assert stored is not None
    assert stored[0] == "started"
    assert stored[1] == "summary"
    assert stored[2] == "allowed"
    assert "TST001" in str(stored[3])

    finished = finish_tool_call(
        _call(), status="completed", result_summary={"count": 2},
        duration_ms=12, completed_at=T2,
    )
    assert repository.complete_tool_call(finished) is True

    stored = connection.execute(
        "SELECT status, duration_ms FROM agent_tool_calls"
    ).fetchone()
    assert stored is not None and stored[0] == "completed" and int(stored[1]) == 12


def test_late_tool_call_completion_is_rejected(wired) -> None:
    """已终止的工具调用不能被再次更新。"""
    repository, _ = wired
    repository.append_message(_message())
    repository.append_tool_call(_call())
    repository.complete_tool_call(finish_tool_call(_call(), status="completed", completed_at=T1))

    assert repository.complete_tool_call(
        finish_tool_call(_call(), status="failed", completed_at=T2)
    ) is False


def test_denied_tool_call_is_persisted(wired) -> None:
    """被拒的调用同样落库，供审计。"""
    repository, connection = wired
    repository.append_message(_message())
    denied = reject_tool_call(
        call_id="88888888-8888-8888-8888-888888888888", session_id=SESSION_ID,
        message_id="44444444-4444-4444-4444-444444444444", ordinal=1,
        tool_name="trades", arguments_summary={}, ledger_version=0, rejected_at=T1,
    )
    repository.append_tool_call(denied)

    stored = connection.execute(
        "SELECT authorization_decision FROM agent_tool_calls"
    ).fetchone()
    assert stored is not None and stored[0] == "denied"


# --------------------------------------------------------------------------
# 事件序号 CAS
# --------------------------------------------------------------------------


def test_event_sequence_bumps_once(wired) -> None:
    """CAS 成功时序号推进。"""
    repository, connection = wired

    assert repository.bump_event_sequence(SESSION_ID, 0, 1) is True
    stored = connection.execute(
        "SELECT last_event_sequence FROM agent_sessions"
    ).fetchone()
    assert stored is not None and int(stored[0]) == 1


def test_stale_event_sequence_bump_fails(wired) -> None:
    """**并发回合的第二个请求必须失败**，而不是写出重复序号。

    用 ``+1`` 直接写会让两个并发回合产生相同序号，客户端看到的事件顺序就不确定。
    """
    repository, connection = wired
    assert repository.bump_event_sequence(SESSION_ID, 0, 1) is True

    assert repository.bump_event_sequence(SESSION_ID, 0, 2) is False

    stored = connection.execute(
        "SELECT last_event_sequence FROM agent_sessions"
    ).fetchone()
    assert stored is not None and int(stored[0]) == 1
