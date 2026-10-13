"""Agent 会话路由测试。

**创建会返回 503**：Agent 运行时（Genkit 执行器）尚未落地，服务端如实报告不可用。
因此创建路径的测试断言的是"如实拒绝"，而不是伪造一个可用状态。

其余五条路由可以独立于执行器验证：读取、审计列表、取消、关闭。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.protocol import CLIENT_PROTOCOL_HEADERS
from lyra.application.device_pairing_service import DevicePairingService
from lyra.application.write_coordinator import WriteCoordinator
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.domain.agent.switching import (
    AgentProviderAvailability,
    create_agent_session,
)
from lyra.domain.analytics.types import AccountScope
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.agent_session_repository import DuckDbAgentSessionRepository
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.main import create_app

PUBLIC_KEY_PEM = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"][0]["inputPem"]
IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")
ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"
SESSION_ID = "33333333-3333-3333-3333-333333333333"
NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)


def _set_status(connection, status: str) -> None:
    """直接把会话改到指定状态（测试用）。"""
    connection.execute(
        "UPDATE agent_sessions SET status = ? WHERE id = CAST(? AS UUID)",
        [status, SESSION_ID],
    )


@pytest.fixture()
def client(tmp_path: Path):
    """装配应用、设备与一个预置会话（状态 idle）。"""
    database_path = tmp_path / "sessions.duckdb"
    database = open_lyra_database(str(database_path))
    connection = database.connection
    connection.execute(
        """
        INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), 'Account', 'TST001', '001')
        """,
        [ACCOUNT_ID],
    )
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
    DuckDbAgentSessionRepository(connection).create_session(
        create_agent_session(
            session_id=SESSION_ID, provider="genkit",
            availability=AgentProviderAvailability(provider="genkit", status="available"),
            account_scope=AccountScope(kind="all"), ledger_version=0, as_of="2026-10-12",
        ),
        device_id=issued.device_id, now=NOW,
    )
    try:
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
            yield test_client, issued
    finally:
        database.close()


def _auth(issued) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {issued.access_token}",
        # **每个 /v1 请求都必须声明客户端协议版本**（除公开路由）。
        # 缺失时服务端返回 426——那是客户端唯一能据此升级 App 的信号。
        "x-lyra-protocol-version": "2",
    }


ALL_ROUTES = [
    ("get", "/v1/agents"),
    ("post", "/v1/agent/sessions"),
    ("get", f"/v1/agent/sessions/{SESSION_ID}"),
    ("get", f"/v1/agent/sessions/{SESSION_ID}/tool-calls"),
    ("post", f"/v1/agent/sessions/{SESSION_ID}/cancel"),
    ("delete", f"/v1/agent/sessions/{SESSION_ID}"),
]


@pytest.mark.parametrize(("method", "path"), ALL_ROUTES)
def test_every_agent_route_requires_authentication(client, method: str, path: str) -> None:
    """六条路由全部要求设备认证。"""
    test_client, _ = client
    response = getattr(test_client, method)(path)
    assert response.status_code == 401, f"{method.upper()} {path} 未要求认证"


def test_create_reports_unavailable_while_the_runtime_is_missing(client) -> None:
    """创建返回 503 并给出稳定原因码，而不是伪造可用状态。"""
    test_client, issued = client
    # **必须发送合法正文**：``accountScope`` 是必填的（参考实现的 schema 无默认值），
    # 因此空正文会先得到 400，走不到可用性检查。
    response = test_client.post(
        "/v1/agent/sessions", headers=_auth(issued),
        json={"provider": "genkit", "accountScope": {"kind": "all"}},
    )

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "AGENT_UNAVAILABLE"
    # **补充信息在 `details` 里**——路由写在 `detail` 的额外键由处理器归入此处。
    assert body["details"] == {"reason": "not_configured"}


def test_read_session_returns_public_snapshot(client) -> None:
    """读取返回公开快照：camelCase、无内部摘要。"""
    test_client, issued = client
    response = test_client.get(f"/v1/agent/sessions/{SESSION_ID}", headers=_auth(issued))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == SESSION_ID
    assert body["status"] == "idle"
    assert body["accountScope"] == {"kind": "all"}
    assert body["messages"] == []
    assert "summary" not in body


def test_read_unknown_session_is_not_found(client) -> None:
    """未知会话返回 404。"""
    test_client, issued = client
    response = test_client.get(
        "/v1/agent/sessions/99999999-9999-9999-9999-999999999999", headers=_auth(issued)
    )
    assert response.status_code == 404
    assert response.json()["code"] == "AGENT_SESSION_NOT_FOUND"


def test_tool_calls_start_empty(client) -> None:
    """新会话的审计列表为空。"""
    test_client, issued = client
    body = test_client.get(
        f"/v1/agent/sessions/{SESSION_ID}/tool-calls", headers=_auth(issued)
    ).json()

    assert body == {"rows": [], "count": 0}


def test_cancel_moves_a_running_session_to_cancelling(client) -> None:
    """取消把 **running** 会话推进到 cancelling。

    **这是两阶段的**：真正的中止由编排器完成；客户端看到 cancelling 表示
    "请求已受理、尚未停止"。
    """
    test_client, issued = client
    _set_status(test_client.app.state.database.connection, "running")

    response = test_client.post(
        f"/v1/agent/sessions/{SESSION_ID}/cancel", headers=_auth(issued)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelling"

    stored = test_client.get(
        f"/v1/agent/sessions/{SESSION_ID}", headers=_auth(issued)
    ).json()
    assert stored["status"] == "cancelling"


def test_cancel_is_rejected_on_an_idle_session(client) -> None:
    """**空闲会话不能"取消"**——没有正在进行的回合。

    取消的语义是"停止正在进行的回合"；空闲会话应当**关闭**（``DELETE``），
    走的是另一条边。这也说明状态机把 ``idle → cancelling`` 定为非法是对的。
    """
    test_client, issued = client

    response = test_client.post(
        f"/v1/agent/sessions/{SESSION_ID}/cancel", headers=_auth(issued)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "AGENT_SESSION_STATE_CONFLICT"
    assert response.json()["details"]["from"] == "idle"


def test_cancelling_session_cannot_be_closed_directly(client) -> None:
    """``cancelling`` 只能回到 ``idle``，不能直接关闭。"""
    test_client, issued = client
    _set_status(test_client.app.state.database.connection, "cancelling")

    response = test_client.delete(
        f"/v1/agent/sessions/{SESSION_ID}", headers=_auth(issued)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "AGENT_SESSION_STATE_CONFLICT"


def test_close_idle_session(client) -> None:
    """idle 会话可以关闭。"""
    test_client, issued = client
    response = test_client.delete(
        f"/v1/agent/sessions/{SESSION_ID}", headers=_auth(issued)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "closed"


def test_close_is_idempotent(client) -> None:
    """重复关闭返回同一结果，而不是报冲突。"""
    test_client, issued = client
    first = test_client.delete(f"/v1/agent/sessions/{SESSION_ID}", headers=_auth(issued))
    second = test_client.delete(f"/v1/agent/sessions/{SESSION_ID}", headers=_auth(issued))

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "closed"


def test_closed_session_cannot_be_cancelled(client) -> None:
    """终态无出边。"""
    test_client, issued = client
    test_client.delete(f"/v1/agent/sessions/{SESSION_ID}", headers=_auth(issued))

    response = test_client.post(
        f"/v1/agent/sessions/{SESSION_ID}/cancel", headers=_auth(issued)
    )

    assert response.status_code == 409
    assert response.json()["details"]["from"] == "closed"


def test_transition_unknown_session_is_not_found(client) -> None:
    """未知会话的转换返回 404。"""
    test_client, issued = client
    for method, path in (
        ("post", "/v1/agent/sessions/99999999-9999-9999-9999-999999999999/cancel"),
        ("delete", "/v1/agent/sessions/99999999-9999-9999-9999-999999999999"),
    ):
        response = getattr(test_client, method)(path, headers=_auth(issued))
        assert response.status_code == 404, f"{method} {path}"


def test_session_responses_have_no_snake_case_keys(client) -> None:
    """公开响应不得出现 snake_case 字段名。"""
    test_client, issued = client

    def snake_keys(value: object, prefix: str = "") -> list[str]:
        found: list[str] = []
        if isinstance(value, dict):
            for key, child in value.items():
                if "_" in key:
                    found.append(f"{prefix}{key}")
                found.extend(snake_keys(child, f"{prefix}{key}."))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                found.extend(snake_keys(child, f"{prefix}[{index}]."))
        return found

    for path in (
        "/v1/agents",
        f"/v1/agent/sessions/{SESSION_ID}",
        f"/v1/agent/sessions/{SESSION_ID}/tool-calls",
    ):
        leaked = snake_keys(test_client.get(path, headers=_auth(issued)).json())
        assert not leaked, f"{path} 泄漏 snake_case 字段: {leaked}"


# --------------------------------------------------------------------------
# 发消息（回合执行）
# --------------------------------------------------------------------------


def _send(client, *, content, session_id: str = SESSION_ID):
    test_client, issued = client
    return test_client.post(
        f"/v1/agent/sessions/{session_id}/messages",
        json={"content": content},
        headers=_auth(issued),
    )


def test_send_message_requires_authentication(client) -> None:
    """发消息必须要求认证。"""
    test_client, _ = client
    assert test_client.post(
        f"/v1/agent/sessions/{SESSION_ID}/messages", json={"content": "x"}
    ).status_code == 401


@pytest.mark.parametrize("payload", [{}, {"content": ""}, {"content": "   "}, {"content": 5}])
def test_send_message_rejects_invalid_body(client, payload: dict) -> None:
    """正文非法必须 400。"""
    test_client, issued = client
    response = test_client.post(
        f"/v1/agent/sessions/{SESSION_ID}/messages",
        json=payload, headers=_auth(issued),
    )
    assert response.status_code == 400


def test_send_message_unknown_session_is_not_found(client) -> None:
    """未知会话 404。"""
    response = _send(client, content="x", session_id="99999999-9999-9999-9999-999999999999")
    assert response.status_code == 404
    assert response.json()["code"] == "AGENT_SESSION_NOT_FOUND"


def test_send_message_requires_an_idle_turn_boundary(client) -> None:
    """非 idle 会话不能开始新回合——否则两个回合会交错。"""
    test_client, _ = client
    _set_status(test_client.app.state.database.connection, "running")

    response = _send(client, content="x")

    assert response.status_code == 409
    assert response.json()["details"]["from"] == "running"


def test_send_message_reports_unavailable_while_the_executor_is_missing(client) -> None:
    """**执行器未接线时返回 503**，而不是把请求当成回合失败。

    Genkit 的 `ModelStream` 绑定尚未落地；如实报告不可用，而不是返回一个
    "回合失败"的空结果让用户以为模型出错了。
    """
    response = _send(client, content="这个月赚了多少")

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "AGENT_UNAVAILABLE"
    # **补充信息在 `details` 里**——路由写在 `detail` 的额外键由处理器归入此处。
    assert body["details"] == {"reason": "not_configured"}


def test_rejected_message_does_not_advance_the_session(client) -> None:
    """被拒的请求不得改动会话状态。"""
    test_client, issued = client
    _send(client, content="x")

    body = test_client.get(
        f"/v1/agent/sessions/{SESSION_ID}", headers=_auth(issued)
    ).json()
    assert body["status"] == "idle"
    assert body["messages"] == []


# --------------------------------------------------------------------------
# 创建会话：参数形状（body 而非查询串）
# --------------------------------------------------------------------------


def _create(test_client, issued, body):
    """按参考实现的正文形状创建会话。"""
    return test_client.post(
        "/v1/agent/sessions", headers=_auth(issued), json=body
    )


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """把模型提供方配置成"可用"。

    可用性检查读**环境变量**，因此不配置就只走得到 503 分支——而本组测试要验证的是
    **正文形状**（一个与运行时是否配置无关的契约）。
    """
    monkeypatch.setenv("LYRA_GENKIT_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("LYRA_GENKIT_MODEL", "test-model")
    monkeypatch.setenv("LYRA_GENKIT_API_KEY", "test-key")


def test_create_accepts_the_reference_body_shape(client, configured) -> None:
    """**参数来自正文**，形状为 ``{"provider", "accountScope": {...}}``。

    此前参数走查询串（扁平 ``?accountId=``），而客户端按参考实现发的是正文——
    结果是**正文被完全忽略**。见下方那条更关键的空范围测试。
    """
    test_client, issued = client
    response = _create(
        test_client, issued, {"provider": "genkit", "accountScope": {"kind": "all"}}
    )

    assert response.status_code == 201
    assert response.json()["accountScope"] == {"kind": "all"}


def test_create_with_an_account_scope_does_not_silently_become_all(client, configured) -> None:
    """**指定账户的会话不得退化成"全部账户"。**

    这是本次修复的核心：正文被忽略时 ``account_id`` 为 ``None``，于是一个"只看 TST001"
    的请求会绑到**全部账户**上——**而且不报错**。对私有账本应用，静默扩大数据范围是
    最危险的一类缺陷。
    """
    test_client, issued = client
    account_id = ACCOUNT_ID
    response = _create(
        test_client, issued,
        {"provider": "genkit", "accountScope": {"kind": "account", "account": account_id}},
    )

    assert response.status_code == 201
    assert response.json()["accountScope"] == {"kind": "account", "account": account_id}
    assert response.json()["accountScope"] != {"kind": "all"}


def test_create_rejects_an_account_scope_without_an_account(client) -> None:
    """``{"kind": "account"}`` 缺 ``account`` 必须 400，**不得退化成全部账户**。"""
    test_client, issued = client
    response = _create(
        test_client, issued, {"provider": "genkit", "accountScope": {"kind": "account"}}
    )

    assert response.status_code == 400


def test_create_rejects_an_empty_body(client) -> None:
    """``accountScope`` 必填——空正文 400（参考实现的 schema 无默认值）。"""
    test_client, issued = client

    assert _create(test_client, issued, {}).status_code == 400


def test_create_rejects_an_unknown_provider(client) -> None:
    """``provider`` 只接受字面量 ``"genkit"``。"""
    test_client, issued = client
    response = _create(
        test_client, issued, {"provider": "openai", "accountScope": {"kind": "all"}}
    )

    assert response.status_code == 400


def test_create_ignores_a_client_supplied_as_of(client, configured) -> None:
    """**数据截止时间取服务端时钟**，客户端指定无效。

    会话基线必须与账本服务看到的时间一致；由客户端决定会让两侧对同一次读取产生
    不同的时点。
    """
    test_client, issued = client
    response = _create(
        test_client, issued,
        {
            "provider": "genkit", "accountScope": {"kind": "all"},
            "asOf": "1999-01-01T00:00:00.000Z",
        },
    )

    assert response.status_code == 201
    assert not response.json()["asOf"].startswith("1999")


# --------------------------------------------------------------------------
# 路径 ID 校验与消息上限
# --------------------------------------------------------------------------


def _path_for(base: str, session_id: str) -> str:
    """把会话 ID 填进路径。"""
    return base.format(session_id=session_id)


@pytest.mark.parametrize(
    "base",
    [
        "/v1/agent/sessions/{session_id}",
        "/v1/agent/sessions/{session_id}/cancel",
        "/v1/agent/sessions/{session_id}/messages",
    ],
)
def test_a_malformed_session_id_is_a_400_not_a_404(client, base: str) -> None:
    """**格式非法的会话 ID 返回 400，而不是 404。**

    这个区分对客户端有意义：400 说"你的请求构造错了"，404 说"这个资源不存在"——
    前者该改代码，后者该刷新列表。参考实现对路径参数用同一个 schema 校验。

    三条按 ID 操作的路由都要有这个行为，因此参数化覆盖。
    """
    test_client, issued = client
    path = _path_for(base, "not-a-uuid")

    # 读取用 GET，另两条用 POST。
    response = (
        test_client.post(path, headers=_auth(issued), json={"content": "hi"})
        if base.endswith("/cancel") or base.endswith("/messages")
        else test_client.get(path, headers=_auth(issued))
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


def test_a_well_formed_but_unknown_session_id_is_a_404(client) -> None:
    """格式合法但不存在的会话仍是 404——**400 与 404 的边界是"格式"而非"存在"**。"""
    test_client, issued = client
    response = test_client.get(
        "/v1/agent/sessions/99999999-9999-9999-9999-999999999999",
        headers=_auth(issued),
    )

    assert response.status_code == 404


def test_a_message_longer_than_the_limit_is_rejected(client) -> None:
    """**单条消息有 8192 字符上限**（与参考实现的 schema 一致）。

    没有上限时，一条超大消息会一路进入模型调用与数据库写入——而客户端的重试与截断
    逻辑正是按那个上限设计的。
    """
    test_client, issued = client
    response = test_client.post(
        f"/v1/agent/sessions/{SESSION_ID}/messages", headers=_auth(issued),
        json={"content": "x" * 8_193},
    )

    assert response.status_code == 400


def test_a_message_at_the_limit_is_accepted(client) -> None:
    """**恰好 8192 字符是合法的**——上限是闭区间。

    边界值值得单独断言：写成 ``>=`` 会拒掉一条合法消息，而这类差一位的错误在
    只有"太长"的测试里永远不会暴露。
    """
    test_client, issued = client
    response = test_client.post(
        f"/v1/agent/sessions/{SESSION_ID}/messages", headers=_auth(issued),
        json={"content": "x" * 8_192},
    )

    # 该会话状态与运行时会决定后续结果，但**不应因长度被拒**。
    assert response.status_code != 400 or response.json().get("code") != "INVALID_ARGUMENT"
