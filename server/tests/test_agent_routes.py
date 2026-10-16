"""Agent 能力发现端点测试。

``GET /v1/agents`` 让 App 判断"服务端能否进入对话界面"。当前 Agent 运行时尚未落地，
因此断言的是**如实报告不可用**，而不是伪造一个可用状态。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.protocol import CLIENT_PROTOCOL_HEADERS
from lyra.application.agent_availability import create_agent_availability_snapshot
from lyra.application.device_pairing_service import DevicePairingService
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.infrastructure.db.user_repository import UserRepository
from lyra.main import create_app

PUBLIC_KEY_PEM = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"][0]["inputPem"]
IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")


@pytest.fixture()
def client(tmp_path: Path):
    database_path = tmp_path / "agents.duckdb"
    database = open_lyra_database(str(database_path))
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    application = create_app(settings, database)
    service = DevicePairingService(
        DuckDbDeviceRepository(database.connection),
        application.state.write_coordinator,
        UserRepository(database.connection),
    )
    issued = asyncio.run(service.authorize_user_device(
        identity=IDENTITY, display_name="Pixel 9", public_key_pem=PUBLIC_KEY_PEM
    ))
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
        # **引用共享常量而不是字面量**：写死会在协议升版后静默变成 426。
        **CLIENT_PROTOCOL_HEADERS,
    }


def test_agents_endpoint_requires_authentication(client) -> None:
    test_client, _ = client
    assert test_client.get("/v1/agents").status_code == 401


def test_agents_reports_unavailable_while_the_runtime_is_missing(client) -> None:
    """Agent 运行时尚未落地，必须**如实报告不可用**。"""
    test_client, issued = client
    body = test_client.get("/v1/agents", headers=_auth(issued)).json()

    assert body["checkedAt"].endswith("Z")
    assert len(body["providers"]) == 1
    provider = body["providers"][0]
    assert provider["provider"] == "genkit"
    assert provider["name"] == "Lyra"
    assert provider["status"] == "unavailable"
    # 稳定原因码，而不是自由文本——客户端需要能稳定分支。
    assert provider["reason"] == "not_configured"


def test_availability_snapshot_is_camel_case_and_stable() -> None:
    """快照字段为 camelCase，且时间由调用方决定。"""
    from datetime import datetime

    now = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
    snapshot = create_agent_availability_snapshot(configured=True, checked_at=now)

    assert snapshot.checked_at == "2026-10-12T09:00:00.000Z"
    assert snapshot.providers[0]["status"] == "available"
    assert snapshot.providers[0]["version"] == "Genkit"
    assert "reason" not in snapshot.providers[0]


def test_availability_rejects_naive_time() -> None:
    """朴素时间会让 checkedAt 带本地时区偏移。"""
    from datetime import datetime

    with pytest.raises(ValueError, match="必须带时区"):
        create_agent_availability_snapshot(
            configured=True, checked_at=datetime(2026, 10, 12, 9, 0, 0)
        )
