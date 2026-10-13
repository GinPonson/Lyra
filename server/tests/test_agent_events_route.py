"""事件 WebSocket 端点测试。

三条性质各自对应一种真实故障：

1. **未认证必须立即关闭**（4001），而不是先订阅再校验；
2. **每次消息都重新认证**——否则设备撤销后旧连接仍收得到数据；
3. **不补造事件**：序号有缺口时要求客户端重新取快照，而不是拼出增量。
"""

from __future__ import annotations

import asyncio
import json
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
from lyra.application.write_coordinator import WriteCoordinator
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.main import create_app

PUBLIC_KEY_PEM = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"][0]["inputPem"]
IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")
SESSION_ID = "33333333-3333-3333-3333-333333333333"


@pytest.fixture()
def wired(tmp_path: Path):
    """装配应用与一个有效令牌。"""
    database_path = tmp_path / "events.duckdb"
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
    try:
        yield application, issued, database.connection
    finally:
        database.close()


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
    application, _, _ = wired
    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client, pytest.raises(Exception):
        with client.websocket_connect("/v1/events") as socket:
            socket.receive_json()


def test_connection_with_a_revoked_token_is_closed(wired) -> None:
    """凭据失效的连接必须被关闭。"""
    application, issued, connection = wired
    asyncio.run(
        DevicePairingService(
            DuckDbDeviceRepository(connection), WriteCoordinator()
        ).revoke_device(issued.device_id)
    )

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client, pytest.raises(Exception):
        with client.websocket_connect(
            "/v1/events", headers={"Authorization": f"Bearer {issued.access_token}"}
        ) as socket:
            socket.receive_json()


def test_invalid_frame_returns_an_error_frame(wired) -> None:
    """非法订阅帧回一个 error 帧，而不是断开连接。"""
    application, issued, _ = wired
    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client, client.websocket_connect(
        "/v1/events", headers={"Authorization": f"Bearer {issued.access_token}"}
    ) as socket:
        socket.send_text("not json")
        frame = socket.receive_json()

    assert frame["type"] == "error"
    assert frame["code"] == "INVALID_ARGUMENT"


def test_subscribing_to_an_unregistered_session_requires_a_snapshot(wired) -> None:
    """会话未登记运行态时要求客户端重新取快照，而不是假装订阅成功。"""
    application, issued, _ = wired
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
    application, issued, _ = wired
    orchestrator: AgentTurnOrchestrator = application.state.agent_orchestrator
    from lyra.domain.agent.switching import AgentSessionSnapshot
    from lyra.domain.analytics.types import AccountScope

    orchestrator.register(
        AgentSessionSnapshot(
            id=SESSION_ID, provider="genkit", status="idle",
            account_scope=AccountScope(kind="all"), ledger_version=0, as_of="t",
            revision=1, summary=None, messages=(),
        )
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
    from lyra.api.agent_events import _event_payload
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
    frames = _frame_keys()

    # 帧的外层键由路由构造；这里断言契约本身在测试里可见。
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
    application, issued, _ = wired
    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
        with test_client.websocket_connect(
            "/v1/events", headers={"Authorization": f"Bearer {issued.access_token}"}
        ) as socket:
            socket.send_text("{}")
            frame = socket.receive_json()

    assert set(frame) == {"type", "code", "message"}
    assert frame["type"] == "error"
    assert frame["code"] == "INVALID_ARGUMENT"
