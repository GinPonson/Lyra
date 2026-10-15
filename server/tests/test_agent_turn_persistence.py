"""Agent 回合在**真实 DuckDB 仓储**上的端到端回归。

**为什么单独一个文件**：这一层此前完全没有覆盖。编排器测试用内存假仓储，
路由测试里 ``agent_executor`` 恒为 ``None``，于是"把领域消息交给仓储"这条真实
路径没人跑过。实测结果是每个真实回合都在第一次落库时抛
``AttributeError: 'NormalizedAgentMessage' object has no attribute 'session_id'``
→ ``POST /messages`` 500，且会话被留在 ``running`` 上永久卡死。

因此这里用**真实仓储 + 真实路由 + 假执行器**固定三件事：

1. 回合能跑完（不再 500），会话回到 ``idle``；
2. 落库的消息 ID 是合法 UUID（``agent_messages.id`` 是 UUID 列）；
3. 回合起始是**原子**的：CAS 失败时不留下"会话 running、没有用户消息"的半写状态。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.application.device_pairing_service import DevicePairingService
from lyra.application.write_coordinator import WriteCoordinator
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.domain.agent.switching import (
    AgentProviderAvailability,
    create_agent_session,
)
from lyra.domain.agent.turn_stream import (
    MessageDelta,
    MessageSnapshot,
    ToolCompleted,
    ToolStarted,
    TurnSettled,
)
from lyra.infrastructure.agents.genkit_runtime import ModelChunk, ModelToolRequest
from lyra.domain.analytics.types import AccountScope
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.agent_session_repository import DuckDbAgentSessionRepository
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.main import create_app
from lyra.protocol import CLIENT_PROTOCOL_HEADERS

PUBLIC_KEY_PEM = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"][0]["inputPem"]
IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")
SESSION_ID = "44444444-4444-4444-4444-444444444444"
NOW = datetime(2026, 10, 13, 9, 0, 0, tzinfo=UTC)

UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class FakeExecutor:
    """产出固定流信号的执行器；形状与 ``GenkitTurnExecutor`` 相同。"""

    def __init__(self) -> None:
        self.contexts: list[dict] = []

    def run(self, *, session, content, context, emit) -> None:
        del session, content
        self.contexts.append(context)
        emit(MessageDelta(delta="净收益 "))
        emit(MessageDelta(delta="200"))
        emit(MessageSnapshot(content="净收益 200", stop_reason="stop"))
        emit(TurnSettled())


@pytest.fixture()
def wired(tmp_path: Path):
    """装配真实应用、真实仓储、已配对设备与一个 idle 会话。"""
    database_path = tmp_path / "turns.duckdb"
    database = open_lyra_database(str(database_path))
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    application = create_app(settings, database)
    service = DevicePairingService(
        DuckDbDeviceRepository(database.connection), WriteCoordinator()
    )
    issued = asyncio.run(service.authorize_owner_device(
        identity=IDENTITY, display_name="Pixel 9", public_key_pem=PUBLIC_KEY_PEM
    ))
    DuckDbAgentSessionRepository(database.connection).create_session(
        create_agent_session(
            session_id=SESSION_ID, provider="genkit",
            availability=AgentProviderAvailability(provider="genkit", status="available"),
            account_scope=AccountScope(kind="all"), ledger_version=0, as_of="2026-10-13",
        ),
        device_id=issued.device_id, now=NOW,
    )
    executor = FakeExecutor()
    application.state.agent_executor = executor
    try:
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
            yield test_client, issued, executor, database
    finally:
        database.close()


def _auth(issued) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {issued.access_token}",
        "x-lyra-protocol-version": "2",
    }


def test_a_turn_completes_against_the_real_store(wired) -> None:
    """**真实仓储下回合必须跑完。** 修复前这里 500，会话卡在 running。"""
    test_client, issued, _, database = wired

    response = test_client.post(
        f"/v1/agent/sessions/{SESSION_ID}/messages",
        headers=_auth(issued), json={"content": "这个月赚了多少"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session"]["status"] == "idle"
    status = database.connection.execute(
        "SELECT status FROM agent_sessions WHERE id = CAST(? AS UUID)", [SESSION_ID]
    ).fetchone()
    assert status == ("idle",), "会话没有回到 idle——回合失败会把它永久留在 running"


def test_persisted_message_ids_are_uuids(wired) -> None:
    """消息 ID 必须是 UUID：``agent_messages.id`` 是 UUID 列。"""
    import re

    test_client, issued, _, database = wired
    test_client.post(
        f"/v1/agent/sessions/{SESSION_ID}/messages",
        headers=_auth(issued), json={"content": "你好"},
    )

    rows = database.connection.execute(
        "SELECT id, ordinal, role, status FROM agent_messages WHERE session_id = CAST(? AS UUID) ORDER BY ordinal",
        [SESSION_ID],
    ).fetchall()
    assert [row[2] for row in rows] == ["user", "assistant"]
    assert [row[1] for row in rows] == [1, 2]
    for row in rows:
        assert re.fullmatch(UUID_PATTERN, str(row[0])), f"消息 ID 不是 UUID: {row[0]}"


def test_the_executor_receives_server_injected_context(wired) -> None:
    """会话的账户范围与账本版本必须由**服务端**注入执行器。

    它们不能来自模型参数：否则一句提示词注入就能把查询扩到全部账户。
    """
    test_client, issued, executor, _ = wired
    test_client.post(
        f"/v1/agent/sessions/{SESSION_ID}/messages",
        headers=_auth(issued), json={"content": "你好"},
    )

    assert len(executor.contexts) == 1
    context = executor.contexts[0]
    assert context.account_scope == AccountScope(kind="all")
    assert context.ledger_version == 0
    assert context.session_id == SESSION_ID
    assert uuid.UUID(context.user_message_id), "上下文必须带已落库的用户消息 ID"


def test_a_rejected_turn_start_leaves_no_half_written_session(wired) -> None:
    """CAS 失败时**不留下任何一行**：既不改会话状态，也不写任何消息。

    这是"分散写"会造成的真实故障：会话被改成 running 却没有任何消息，之后
    ``send_message`` 恒 409、``DELETE`` 又不允许 ``running→closed``——只能手工改库。
    """
    from lyra.application.agent_turn_orchestrator import AgentTurnOrchestrator
    from lyra.infrastructure.db.agent_turn_store import DuckDbAgentTurnStore

    _, _, _, database = wired
    repository = DuckDbAgentSessionRepository(database.connection)
    store = DuckDbAgentTurnStore(database.connection)
    orchestrator = AgentTurnOrchestrator(store=store, now=lambda: NOW)
    # 登记一个 **过期** 的 revision：起始写入的 CAS 必然失配。
    snapshot = repository.load_session(SESSION_ID)
    assert snapshot is not None
    orchestrator.register(_with_revision(snapshot, snapshot.revision + 5))

    with pytest.raises(ValueError, match="并发修改"):
        orchestrator.run_turn(
            session_id=SESSION_ID, executor=FakeExecutor(), content="x"
        )

    status = database.connection.execute(
        "SELECT status FROM agent_sessions WHERE id = CAST(? AS UUID)", [SESSION_ID]
    ).fetchone()
    messages = database.connection.execute(
        "SELECT count(*) FROM agent_messages WHERE session_id = CAST(? AS UUID)",
        [SESSION_ID],
    ).fetchone()
    assert status == ("idle",), "CAS 失败却改动了会话状态（半写）"
    assert messages == (0,), "CAS 失败却写入了用户消息（半写）"


def _with_revision(row, revision: int):
    """按仓储行重建一个指定 revision 的领域快照（仅测试用）。"""
    from lyra.domain.agent.switching import AgentSessionSnapshot

    return AgentSessionSnapshot(
        id=row.id, provider=row.provider, status=row.status,
        account_scope=row.account_scope, ledger_version=row.ledger_version,
        as_of=row.as_of, revision=revision, summary=row.summary, messages=(),
    )


class ToolCallingExecutor:
    """先请求一次工具，再给出最终回答。"""

    def __init__(self) -> None:
        self.ordinals: list[int] = []

    def run(self, *, session, content, context, emit) -> None:
        del session, content
        # 工具调用挂在**用户消息**上（助手消息要到回合末才落库），因此外键只能指向它。
        call_id = str(uuid.uuid4())
        emit(ToolStarted(
            message_id=context.user_message_id, call_id=call_id, ordinal=1,
            tool_name="summary", authorization_decision="allowed", status="started",
            arguments_summary={"scope": "all"}, result_summary=None,
            duration_ms=None, error_code=None, ledger_version=context.ledger_version,
        ))
        emit(ToolCompleted(
            message_id=context.user_message_id, call_id=call_id, ordinal=1,
            tool_name="summary", authorization_decision="allowed", status="completed",
            arguments_summary={"scope": "all"}, result_summary={"rows": 1},
            duration_ms=12, error_code=None, ledger_version=context.ledger_version,
        ))
        emit(MessageSnapshot(content="答案", stop_reason="stop"))
        emit(TurnSettled())


def test_tool_calls_are_audited_on_the_real_store(wired) -> None:
    """工具调用必须**落进审计表**，而不只是发一条事件。

    修复前 ``append_tool_call``/``complete_tool_call`` 在整个 ``src/`` 里零调用者：
    客户端能看到 ``tool.completed`` 事件，但回合结束后 ``GET .../tool-calls`` 永远为空
    ——"发生过什么"没有留下任何可查记录。
    """
    test_client, issued, _, database = wired
    test_client.app.state.agent_executor = ToolCallingExecutor()

    response = test_client.post(
        f"/v1/agent/sessions/{SESSION_ID}/messages",
        headers=_auth(issued), json={"content": "给我总览"},
    )
    assert response.status_code == 200, response.text

    rows = database.connection.execute(
        """
        SELECT tool_name, status, authorization_decision, duration_ms, error_code,
               ordinal, message_id
        FROM agent_tool_calls WHERE session_id = CAST(? AS UUID)
        """,
        [SESSION_ID],
    ).fetchall()
    assert len(rows) == 1, rows
    assert rows[0][0] == "summary"
    assert rows[0][1] == "completed"
    assert rows[0][2] == "allowed"
    assert rows[0][3] == 12
    assert rows[0][4] is None
    assert rows[0][5] == 1, "工具调用序号必须为正"

    # 审计行挂在**用户**消息上：助手消息这一列在接口里也必须能查到同一条调用。
    user_message = database.connection.execute(
        "SELECT id FROM agent_messages WHERE session_id = CAST(? AS UUID) AND role = 'user'",
        [SESSION_ID],
    ).fetchone()
    assert str(rows[0][6]) == str(user_message[0])

    listed = test_client.get(
        f"/v1/agent/sessions/{SESSION_ID}/tool-calls", headers=_auth(issued)
    ).json()
    assert [call["toolName"] for call in listed["toolCalls"]] == ["summary"]
    assert listed["toolCalls"][0]["status"] == "completed"


def test_a_rejected_tool_call_is_audited_as_rejected(wired) -> None:
    """被策略拒绝的调用也要留痕：事后必须能回答"模型尝试过什么"。"""
    test_client, issued, _, database = wired

    class RejectingExecutor:
        def run(self, *, session, content, context, emit) -> None:
            del session, content
            emit(ToolCompleted(
                message_id=context.user_message_id, call_id=str(uuid.uuid4()), ordinal=1,
                tool_name="summary", authorization_decision="denied", status="rejected",
                arguments_summary={}, result_summary=None, duration_ms=0,
                error_code="AGENT_TOOL_FORBIDDEN",
                ledger_version=context.ledger_version,
            ))
            emit(MessageSnapshot(content="无法执行该调用", stop_reason="stop"))
            emit(TurnSettled())

    test_client.app.state.agent_executor = RejectingExecutor()
    response = test_client.post(
        f"/v1/agent/sessions/{SESSION_ID}/messages",
        headers=_auth(issued), json={"content": "x"},
    )
    assert response.status_code == 200, response.text

    row = database.connection.execute(
        "SELECT status, authorization_decision, error_code FROM agent_tool_calls "
        "WHERE session_id = CAST(? AS UUID)",
        [SESSION_ID],
    ).fetchone()
    assert row == ("rejected", "denied", "AGENT_TOOL_FORBIDDEN")


# --------------------------------------------------------------------------
# 真实执行器的对接（编排器 ↔ GenkitTurnExecutor）
# --------------------------------------------------------------------------


def test_the_real_executor_completes_a_turn_through_the_orchestrator(tmp_path: Path) -> None:
    """**真实执行器**（不是替身）必须能跑完一个回合并落审计。

    这条覆盖的是一个测试盲区：编排器测试用 ``FakeExecutor``、运行时测试直接调
    ``run_agent_turn_stream``，于是"编排器调用真实执行器"这一步没有测试看着。
    实测过的事故是签名漂移（执行器删掉 ``message_id`` 后编排器仍在传），
    ``pytest`` 全绿但真实链路 ``TypeError``。
    """
    from lyra.application.agent_config import GenkitConfig
    from lyra.application.agent_turn_orchestrator import AgentTurnOrchestrator
    from lyra.application.tool_runtime import ToolDefinition
    from lyra.infrastructure.agents.genkit_runtime import GenkitTurnExecutor
    from lyra.infrastructure.db.agent_turn_store import DuckDbAgentTurnStore

    class ScriptedStream:
        """一次模型调用：要么请求工具，要么给出最终文本。"""

        def __init__(self, *, tools=(), final="") -> None:
            self._tools = tuple(tools)
            self._final = final

        def chunks(self):
            return [] if self._tools else [ModelChunk(text=self._final)]

        def tool_requests(self):
            return self._tools

        def usage(self):
            return None

        def final_text(self):
            return "" if self._tools else self._final

    class ScriptedProvider:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, **kwargs):
            del kwargs
            self.calls += 1
            if self.calls == 1:
                return ScriptedStream(tools=(ModelToolRequest(
                    name="summary", arguments={"scope": "all"},
                ),))
            return ScriptedStream(final="净收益 200")

    calls: list[dict] = []

    def handler(arguments, context):
        calls.append({"arguments": dict(arguments), "scope": context.account_scope})
        return {"netDeposits": "200"}

    definitions = (ToolDefinition(
        name="summary", description="总览", input_schema={}, handler=handler,
    ),)
    provider = ScriptedProvider()
    executor = GenkitTurnExecutor(
        config=GenkitConfig(
            base_url="http://127.0.0.1:9/v1", model="m", api_key="k",
            turn_timeout_ms=1_000, max_turns=3,
        ),
        tool_definitions=definitions,
        stream_provider=provider,
    )

    database = open_lyra_database(str(tmp_path / "real.duckdb"))
    try:
        repository = DuckDbAgentSessionRepository(database.connection)
        device_id = "55555555-5555-5555-5555-555555555555"
        # 会话对设备有外键，测试里直接落一行最小设备（不必走签发流程）。
        database.connection.execute(
            """
            INSERT INTO paired_devices (
                id, display_name, public_key_pem, public_key_fingerprint,
                key_algorithm, status, paired_at, updated_at
            ) VALUES (
                CAST(? AS UUID), 'Pixel 9', 'pem', ?, 'rsa', 'active',
                CAST(? AS TIMESTAMP), CAST(? AS TIMESTAMP)
            )
            """,
            [device_id, "b" * 64, "2026-10-13T09:00:00", "2026-10-13T09:00:00"],
        )
        repository.create_session(
            create_agent_session(
                session_id=SESSION_ID, provider="genkit",
                availability=AgentProviderAvailability(provider="genkit", status="available"),
                account_scope=AccountScope(kind="all"), ledger_version=0, as_of="2026-10-13",
            ),
            device_id=device_id, now=NOW,
        )
        orchestrator = AgentTurnOrchestrator(
            store=DuckDbAgentTurnStore(database.connection), now=lambda: NOW
        )
        row = repository.load_session(SESSION_ID)
        assert row is not None
        orchestrator.register(_with_revision(row, row.revision), device_id=row.device_id)

        result = orchestrator.run_turn(
            session_id=SESSION_ID, executor=executor, content="这个月赚了多少"
        )

        assert result.snapshot.status == "idle"
        assert [event.type for event in result.events] == [
            "message.started", "tool.started", "tool.completed",
            "message.delta", "message.completed",
        ]
        # 工具结果被回灌 → 模型进行了第二次调用。
        assert provider.calls == 2
        assert len(calls) == 1
        assert calls[0]["scope"] == AccountScope(kind="all")

        audited = database.connection.execute(
            "SELECT tool_name, status FROM agent_tool_calls WHERE session_id = CAST(? AS UUID)",
            [SESSION_ID],
        ).fetchall()
        assert audited == [("summary", "completed")]
        messages = database.connection.execute(
            "SELECT role, status, content FROM agent_messages WHERE session_id = CAST(? AS UUID) ORDER BY ordinal",
            [SESSION_ID],
        ).fetchall()
        assert messages[0][:2] == ("user", "completed")
        assert messages[1][0] == "assistant" and messages[1][2] == "净收益 200"
    finally:
        database.close()


def test_real_store_history_excludes_the_current_user_message(wired) -> None:
    """连续两轮真实仓储请求带入旧对话，且不把本轮 user 重复发送给模型。"""
    from lyra.application.agent_config import GenkitConfig
    from lyra.infrastructure.agents.genkit_runtime import GenkitTurnExecutor

    class FinalStream:
        """不调用工具、直接返回固定最终文本的模型流。"""

        def __init__(self, answer: str) -> None:
            self._answer = answer

        def chunks(self):
            return [ModelChunk(text=self._answer)]

        def tool_requests(self):
            return ()

        def usage(self):
            return None

        def final_text(self):
            return self._answer

    class CapturingProvider:
        """记录每轮传给模型的历史，并返回不同的固定答案。"""

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            return FinalStream(f"回答 {len(self.calls)}")

    test_client, issued, _, database = wired
    provider = CapturingProvider()
    test_client.app.state.agent_executor = GenkitTurnExecutor(
        config=GenkitConfig(
            base_url="http://127.0.0.1:9/v1", model="test", api_key="test-key",
            turn_timeout_ms=1_000, max_turns=2,
        ),
        tool_definitions=(),
        stream_provider=provider,
    )

    for _ in range(2):
        response = test_client.post(
            f"/v1/agent/sessions/{SESSION_ID}/messages",
            headers=_auth(issued), json={"content": "重复内容"},
        )
        assert response.status_code == 200, response.text

    rows = database.connection.execute(
        "SELECT id, role, content FROM agent_messages "
        "WHERE session_id = CAST(? AS UUID) ORDER BY ordinal",
        [SESSION_ID],
    ).fetchall()
    assert [(row[1], row[2]) for row in rows] == [
        ("user", "重复内容"),
        ("assistant", "回答 1"),
        ("user", "重复内容"),
        ("assistant", "回答 2"),
    ]
    assert provider.calls[0]["history"] == ()
    history = provider.calls[1]["history"]
    assert [(message.id, message.role, message.content) for message in history] == [
        (str(rows[0][0]), "user", "重复内容"),
        (str(rows[1][0]), "assistant", "回答 1"),
    ]
    assert all(message.id != str(rows[2][0]) for message in history)
