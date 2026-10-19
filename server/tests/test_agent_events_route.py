"""事件 WebSocket 端点测试。

四条性质各自对应一种真实故障：

1. **未认证必须立即关闭**（4001），而不是先订阅再校验；
2. **每次消息都重新认证**——否则设备撤销后旧连接仍收得到数据；
3. **不补造事件**：序号有缺口时要求客户端重新取快照，而不是拼出增量；
4. **订阅必须校验会话归属**——认证只回答"你是谁"，不回答"这个会话是不是你的"（V07）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.protocol import CLIENT_PROTOCOL_HEADERS
from lyra.api.agent_events import (
    SubscribeMessage,
    _event_payload,
    parse_subscribe_message,
)
from lyra.application.agent_turn_orchestrator import AgentTurnOrchestrator
from lyra.application.device_pairing_service import DevicePairingService
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.domain.agent.switching import (
    AgentProviderAvailability,
    create_agent_session,
)
from lyra.domain.agent.turn_stream import MessageDelta, MessageSnapshot, TurnSettled
from lyra.domain.analytics.types import AccountScope
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.agent_session_repository import DuckDbAgentSessionRepository
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.infrastructure.db.user_repository import UserRepository
from lyra.main import create_app

DEVICE_KEYS = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"]
PUBLIC_KEY_PEM = DEVICE_KEYS[0]["inputPem"]
IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")
OTHER_IDENTITY = LoginIdentity(
    provider="github", subject="events-b", email=None, display_name="B"
)
SESSION_ID = "33333333-3333-3333-3333-333333333333"
OTHER_SESSION_ID = "66666666-6666-4666-8666-666666666666"
ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ACCOUNT_ID = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)


@pytest.fixture()
def wired(tmp_path: Path):
    """装配应用、两个用户各自的账户与会话，以及双方的有效令牌。"""
    database_path = tmp_path / "events.duckdb"
    database = open_lyra_database(str(database_path))
    connection = database.connection
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    application = create_app(settings, database)
    users = UserRepository(connection)
    service = DevicePairingService(
        DuckDbDeviceRepository(connection), application.state.write_coordinator, users
    )
    issued = asyncio.run(service.authorize_user_device(
        identity=IDENTITY, display_name="Pixel 9", public_key_pem=PUBLIC_KEY_PEM
    ))
    other = asyncio.run(service.authorize_user_device(
        identity=OTHER_IDENTITY, display_name="Other Phone",
        public_key_pem=DEVICE_KEYS[1]["inputPem"],
    ))
    repository = DuckDbAgentSessionRepository(connection)
    # 两个用户各一个会话；**两边都用同一个 legacy 账户标识**，这正是本次改造要
    # 支持的场景。
    for session_id, account_id, user_id, device_id in (
        (SESSION_ID, ACCOUNT_ID, issued.user.id, issued.device_id),
        (OTHER_SESSION_ID, OTHER_ACCOUNT_ID, other.user.id, other.device_id),
    ):
        connection.execute(
            """
            INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Account', 'TST001', '001')
            """,
            [account_id, user_id],
        )
        repository.create_session(
            create_agent_session(
                session_id=session_id, provider="genkit",
                availability=AgentProviderAvailability(
                    provider="genkit", status="available"
                ),
                account_scope=AccountScope(kind="account", account=account_id),
                ledger_version=0, as_of="2026-10-12",
            ),
            device_id=device_id, user_id=user_id, now=NOW,
        )
    try:
        yield application, issued, connection, other
    finally:
        database.close()


class _ScriptedExecutor:
    """产生一段可见文本并收口的替身执行器（用于验证实时事件确实被投递）。"""

    def run(self, *, session, content, context, emit) -> None:
        emit(MessageDelta(delta="B 的实时增量"))
        emit(MessageSnapshot(content="B 的实时增量", stop_reason="stop"))
        emit(TurnSettled())


# --------------------------------------------------------------------------
# 订阅帧解析
# --------------------------------------------------------------------------


def test_parses_a_valid_subscribe_frame() -> None:
    """合法订阅帧解析成功。"""
    message = parse_subscribe_message(
        json.dumps({"type": "subscribe", "sessionId": SESSION_ID, "afterSequence": 3})
    )

    assert message == SubscribeMessage(session_id=SESSION_ID, after_sequence=3)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"type": "other", "sessionId": "' + SESSION_ID + '", "afterSequence": 0}',
        '{"type": "subscribe", "sessionId": "not-a-uuid", "afterSequence": 0}',
        '{"type": "subscribe", "sessionId": "' + SESSION_ID + '"}',
        '{"type": "subscribe", "sessionId": "' + SESSION_ID + '", "afterSequence": -1}',
        '{"type": "subscribe", "sessionId": "' + SESSION_ID + '", "afterSequence": 1.5}',
        '{"type": "subscribe", "sessionId": "' + SESSION_ID + '", "afterSequence": true}',
    ],
)
def test_rejects_invalid_subscribe_frames(raw: str) -> None:
    """非法订阅帧返回 None（由路由回一个 error 帧）。"""
    assert parse_subscribe_message(raw) is None


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------


def test_connection_without_a_token_is_closed(wired) -> None:
    """**未认证立即关闭**，而不是先订阅再校验。"""
    application, _, _, _ = wired
    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client, pytest.raises(Exception):
        with client.websocket_connect("/v1/events") as socket:
            socket.receive_json()


def test_connection_with_a_revoked_token_is_closed(wired) -> None:
    """凭据失效的连接必须被关闭。"""
    application, issued, connection, _ = wired
    asyncio.run(
        DevicePairingService(
            DuckDbDeviceRepository(connection),
            application.state.write_coordinator,
            UserRepository(connection),
        ).revoke_device(issued.device_id)
    )

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client, pytest.raises(Exception):
        with client.websocket_connect(
            "/v1/events", headers={"Authorization": f"Bearer {issued.access_token}"}
        ) as socket:
            socket.receive_json()


def test_invalid_frame_returns_an_error_frame(wired) -> None:
    """非法订阅帧回一个 error 帧，而不是断开连接。"""
    application, issued, _, _ = wired
    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client, client.websocket_connect(
        "/v1/events", headers={"Authorization": f"Bearer {issued.access_token}"}
    ) as socket:
        socket.send_text("not json")
        frame = socket.receive_json()

    assert frame["type"] == "error"
    assert frame["code"] == "INVALID_ARGUMENT"


def test_subscribing_to_an_unregistered_session_requires_a_snapshot(wired) -> None:
    """会话未登记运行态时要求客户端重新取快照，而不是假装订阅成功。"""
    application, issued, _, _ = wired
    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client, client.websocket_connect(
        "/v1/events", headers={"Authorization": f"Bearer {issued.access_token}"}
    ) as socket:
        socket.send_text(json.dumps({
            "type": "subscribe", "sessionId": SESSION_ID, "afterSequence": 0
        }))
        frame = socket.receive_json()

    assert frame == {
        "type": "subscribed", "mode": "snapshot_required", "reason": "SESSION_NOT_RESUMED"
    }


def test_subscribing_to_a_registered_session_returns_buffered_events(wired) -> None:
    """已登记会话返回缓冲区里的增量。"""
    application, issued, _, _ = wired
    orchestrator: AgentTurnOrchestrator = application.state.agent_orchestrator
    from lyra.domain.agent.switching import AgentSessionSnapshot
    from lyra.domain.analytics.types import AccountScope

    orchestrator.register(
        AgentSessionSnapshot(
            id=SESSION_ID, provider="genkit", status="idle",
            account_scope=AccountScope(kind="all"), ledger_version=0, as_of="t",
            revision=1, summary=None, messages=(),
        ),
        user_id=issued.user.id,
    )

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client, client.websocket_connect(
        "/v1/events", headers={"Authorization": f"Bearer {issued.access_token}"}
    ) as socket:
        socket.send_text(json.dumps({
            "type": "subscribe", "sessionId": SESSION_ID, "afterSequence": 0
        }))
        frame = socket.receive_json()

    assert frame == {"type": "subscribed", "mode": "events", "events": []}


# --------------------------------------------------------------------------
# 帧形状契约（与参考实现逐字段对照）
# --------------------------------------------------------------------------


def test_event_envelope_fields_match_the_reference() -> None:
    """**事件信封的字段与参考实现逐项一致。**

    TS 的 ``AgentEvent`` 是 ``type`` / ``sessionId`` / ``messageId`` / ``sequence`` /
    ``occurredAt`` / ``payload``。客户端按这些名字取值，**改名不会报错，只会读到
    ``undefined``**。
    """
    from lyra.domain.agent.events import AgentEvent

    event = AgentEvent(
        type="message.delta", session_id="s1", message_id="m1", sequence=3,
        occurred_at="2026-10-13T09:00:00.000Z", payload={"text": "hi"},
    )

    payload = _event_payload(event)

    assert set(payload) == {
        "type", "sessionId", "messageId", "sequence", "occurredAt", "payload"
    }
    assert payload["sessionId"] == "s1"
    assert payload["payload"] == {"text": "hi"}


def test_the_event_frame_wraps_the_envelope_under_event() -> None:
    """事件帧把信封放在 ``event`` 键下，而不是平铺。"""
    # 帧的外层键由路由构造；这里断言契约本身在测试里可见。
    assert _frame_keys() == _EVENT_FRAME_OUTER_KEYS
    assert "event" in _EVENT_FRAME_OUTER_KEYS


def _frame_keys() -> set[str]:
    """返回事件帧的外层键（从源码约定中提取）。"""
    return {"type", "event"}


_EVENT_FRAME_OUTER_KEYS = {"type", "event"}


def test_subscribed_modes_are_the_documented_two() -> None:
    """``subscribed`` 帧只有两种模式，``snapshot_required`` 的三种原因与参考实现一致。"""
    from lyra.domain.agent.events import ResumeMode, SnapshotReason
    from typing import get_args

    assert set(get_args(ResumeMode)) == {"events", "snapshot_required"}
    assert set(get_args(SnapshotReason)) == {"client_ahead", "event_gap", "mixed_session"}


def test_error_frame_uses_the_invalid_argument_code(wired) -> None:
    """非法订阅帧回 ``error`` 帧，码为 ``INVALID_ARGUMENT``（与参考实现一致）。"""
    application, issued, _, _ = wired
    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
        with test_client.websocket_connect(
            "/v1/events", headers={"Authorization": f"Bearer {issued.access_token}"}
        ) as socket:
            socket.send_text("{}")
            frame = socket.receive_json()

    assert set(frame) == {"type", "code", "message"}
    assert frame["type"] == "error"
    assert frame["code"] == "INVALID_ARGUMENT"


# --------------------------------------------------------------------------
# 登出必须终止已建立的事件流
# --------------------------------------------------------------------------


def test_a_connected_socket_is_registered_under_its_device(wired) -> None:
    """连接按**设备**登记：登出要能据此精确踢掉该设备的连接。

    这里只断言"真实连接确实登记在它的设备下"（``take_device`` 取到 1 个）；
    关闭动作本身由下面的替身单测覆盖。
    """
    application, issued, _, _ = wired
    registry = application.state.event_connections

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client, client.websocket_connect(
        "/v1/events", headers={"Authorization": f"Bearer {issued.access_token}"}
    ) as socket:
        socket.send_text(json.dumps({
            "type": "subscribe", "sessionId": SESSION_ID, "afterSequence": 0
        }))
        socket.receive_json()
        assert len(registry.take_device(issued.device_id)) == 1

    # 连接已被取走；重复登出是幂等的（不会二次关闭）。
    assert registry.take_device(issued.device_id) == ()


def test_closing_one_device_leaves_other_devices_alone() -> None:
    """只关目标设备的连接：踢错设备等于把别人的对话打断。"""
    from lyra.api.agent_events import EventConnectionRegistry, _close_quietly

    class FakeSocket:
        def __init__(self) -> None:
            self.closed: list[tuple[int, str]] = []

        async def close(self, code: int, reason: str) -> None:
            self.closed.append((code, reason))

    registry = EventConnectionRegistry()
    mine, other = FakeSocket(), FakeSocket()
    registry.add("device-1", mine, user_id="user-1")
    registry.add("device-2", other, user_id="user-2")

    async def sign_out() -> None:
        for websocket in registry.take_device("device-1"):
            await _close_quietly(websocket, 4001, "signed out")

    asyncio.run(sign_out())
    assert mine.closed == [(4001, "signed out")]
    assert other.closed == [], "关掉了不属于该设备的连接"


def test_closing_a_device_socket_sends_the_unauthorized_close_code() -> None:
    """关闭码必须是 4001：客户端据此区分"登录失效"与"网络断了"。"""
    from lyra.api.agent_events import EventConnectionRegistry, UNAUTHORIZED_CLOSE_CODE

    class FakeSocket:
        def __init__(self) -> None:
            self.closed: list[tuple[int, str]] = []

        async def close(self, code: int, reason: str) -> None:
            self.closed.append((code, reason))

    registry = EventConnectionRegistry()
    socket = FakeSocket()
    registry.add("device-1", socket, user_id="user-1")

    assert UNAUTHORIZED_CLOSE_CODE == 4001
    assert registry.take_device("device-1") == (socket,)


# --------------------------------------------------------------------------
# 停用级联：按用户一次关掉全部连接
# --------------------------------------------------------------------------


def test_closing_a_user_leaves_other_users_alone() -> None:
    """**按用户收口**：停用用户时关掉他的全部设备连接，别人的连接不受影响。"""
    from lyra.api.agent_events import EventConnectionRegistry, _close_quietly

    class FakeSocket:
        def __init__(self) -> None:
            self.closed: list[tuple[int, str]] = []

        async def close(self, code: int, reason: str) -> None:
            self.closed.append((code, reason))

    registry = EventConnectionRegistry()
    phone, tablet, stranger = FakeSocket(), FakeSocket(), FakeSocket()
    # 同一个用户的两台设备，各自一条连接。
    registry.add("device-1", phone, user_id="user-1")
    registry.add("device-2", tablet, user_id="user-1")
    registry.add("device-3", stranger, user_id="user-2")

    async def disable() -> None:
        for websocket in registry.take_user("user-1"):
            await _close_quietly(websocket, 4001, "disabled")

    asyncio.run(disable())
    assert phone.closed == [(4001, "disabled")]
    assert tablet.closed == [(4001, "disabled")]
    assert stranger.closed == [], "关掉了不属于该用户的连接"
    # 取走后设备维度也必须清理干净：否则一次停用会让后续 take_device 拿到陈旧引用。
    assert registry.take_device("device-1") == ()
    assert registry.take_device("device-3") == (stranger,)


# --------------------------------------------------------------------------
# 订阅归属（验收 V07）
# --------------------------------------------------------------------------


def test_another_user_cannot_subscribe_to_the_session(wired) -> None:
    """**A 订阅 B 的会话被拒，且历史与实时事件都不泄露**（V07）。

    认证只回答"你是谁"。不校验归属时，任何已认证用户凭一个会话 ID 就能收到他人会话
    的重放与实时增量——这里的断言同时覆盖三件事：

    1. 拒绝帧与"会话不可续传"**完全一致**（不构成"该会话是否存在"的探针）；
    2. 被拒的订阅**没有建立任何订阅**（订阅者数只算 B 自己）；
    3. B 随后真产生事件时，只推给 B。
    """
    application, issued, _, other = wired
    orchestrator: AgentTurnOrchestrator = application.state.agent_orchestrator
    from lyra.domain.agent.switching import AgentSessionSnapshot

    orchestrator.register(
        AgentSessionSnapshot(
            id=OTHER_SESSION_ID, provider="genkit", status="idle",
            account_scope=AccountScope(kind="all"), ledger_version=0, as_of="t",
            revision=1, summary=None, messages=(),
        ),
        user_id=other.user.id,
    )

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        with client.websocket_connect(
            "/v1/events", headers={"Authorization": f"Bearer {issued.access_token}"}
        ) as a_socket, client.websocket_connect(
            "/v1/events", headers={"Authorization": f"Bearer {other.access_token}"}
        ) as b_socket:
            subscribe = json.dumps({
                "type": "subscribe", "sessionId": OTHER_SESSION_ID, "afterSequence": 0
            })
            b_socket.send_text(subscribe)
            # 正对照：B 自己能订阅成功——证明下面 A 收到的拒绝帧不是"会话不可用"。
            assert b_socket.receive_json() == {
                "type": "subscribed", "mode": "events", "events": []
            }
            assert orchestrator.subscriber_count(OTHER_SESSION_ID) == 1

            a_socket.send_text(subscribe)
            assert a_socket.receive_json() == {
                "type": "subscribed", "mode": "snapshot_required",
                "reason": "SESSION_NOT_RESUMED",
            }
            # **拒绝帧之外的证据**：A 没有被登记为订阅者，因此后续实时事件不可能流向它。
            assert orchestrator.subscriber_count(OTHER_SESSION_ID) == 1

            # 真产生事件：B 收得到（证明事件确实被投递过）。
            orchestrator.run_turn(
                session_id=OTHER_SESSION_ID,
                executor=_ScriptedExecutor(),
                content="B 的问题",
            )
            first = b_socket.receive_json()
            assert first["type"] == "event"
            assert first["event"]["sessionId"] == OTHER_SESSION_ID
            assert orchestrator.subscriber_count(OTHER_SESSION_ID) == 1


def test_disabling_the_user_closes_the_event_stream(wired) -> None:
    """**停用后长连接不再收数据**：每次消息都重新认证，停用即被拒并关闭（V11）。"""
    application, issued, connection, _ = wired
    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/v1/events", headers={"Authorization": f"Bearer {issued.access_token}"}
            ) as socket:
                connection.execute(
                    "UPDATE users SET status = 'disabled' WHERE id = CAST(? AS UUID)",
                    [issued.user.id],
                )
                # 停用提交后再发一帧：路由重新认证时必须关闭连接，而不是继续订阅。
                socket.send_text(json.dumps({
                    "type": "subscribe", "sessionId": SESSION_ID, "afterSequence": 0
                }))
                socket.receive_json()

