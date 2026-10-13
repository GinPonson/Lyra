"""端点契约测试。

覆盖 TypeScript 版 ``/v1/health`` 与 ``/v1/system/info`` 的对外契约：
状态码、字段名与语义必须一致，否则 M9 切流时 Flutter 端会立刻出现差异。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.protocol import CLIENT_PROTOCOL_HEADERS
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.db.migrations import CURRENT_SCHEMA_VERSION
from lyra.main import create_app

MASTER_KEY = "0" * 64


def _settings(database_path: Path) -> Settings:
    """构造测试用配置；字段名与部署环境完全一致。"""
    return Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(database_path.parent / "staging"),
        LYRA_SECRET_MASTER_KEY=MASTER_KEY,
    )


@pytest.fixture()
def client(tmp_path: Path):
    """把应用挂在临时账本上，测试结束后关闭数据库。"""
    database_path = tmp_path / "api.duckdb"
    database = open_lyra_database(str(database_path))
    application = create_app(_settings(database_path), database)
    try:
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
            yield test_client
    finally:
        database.close()


def test_health_reports_available_database(client: TestClient) -> None:
    """健康端点应报告可用数据库与实际 schema 版本。"""
    response = client.get("/v1/health")

    assert response.status_code == 200
    body = response.json()
    # **夹具里 Agent 运行时未装配，因此整体是 degraded（不是 healthy）。**
    # 参考实现的判定是 agentStatus !== "available"，而运行时缺席映射为 unknown
    # —— unknown 同样算降级。这与"数据库不可用 → unavailable"是两回事：
    # 服务能工作，只是对话能力未知。
    assert body["status"] == "degraded"
    assert body["process"] == {"status": "available"}
    assert body["database"] == {
        "status": "available",
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "ledgerVersion": 0,
    }
    # checkedAt 必须是 UTC ISO 时间戳，与 JavaScript toISOString() 同形。
    assert body["checkedAt"].endswith("Z")


def test_health_reports_all_seven_sections(client: TestClient) -> None:
    """**健康快照的七段现在都已实现。**

    这条测试曾断言 ``writeQueue`` / ``gmail`` / ``agents`` **不存在**——那是功能未实现
    时的守则（宁可缺段，也不填占位值冒充可用）。三段现已实现，因此断言改为
    **它们存在且取值真实**。

    原则没变：**结构可以缺，但绝不能假**。
    """
    body = client.get("/v1/health").json()

    assert set(body) == {
        "status", "checkedAt", "process", "database",
        "writeQueue", "gmail", "agents",
    }
    assert body["writeQueue"]["status"] in ("available", "draining")
    assert isinstance(body["writeQueue"]["pendingOperations"], int)
    assert body["gmail"]["status"] in ("available", "degraded")
    assert isinstance(body["gmail"]["totalConnections"], int)
    assert body["gmail"]["attentionRequired"] >= 0
    assert body["agents"]["status"] in ("available", "degraded", "unknown")


def test_health_status_reflects_degraded_not_only_database(client: TestClient) -> None:
    """**``degraded`` 是独立于数据库的一种状态。**

    Agent 未配置时整体状态是 ``degraded``（服务能工作，只是对话能力不可用），
    **而不是** ``unavailable``——把后者报成前者会让客户端放弃一个其实可用的服务。

    同时断言 ``degraded`` 仍是 **HTTP 200**。
    """
    response = client.get("/v1/health")
    body = response.json()

    if body["agents"]["status"] != "available":
        assert body["status"] == "degraded"
        assert response.status_code == 200


def test_health_returns_503_when_database_unavailable(tmp_path: Path) -> None:
    """数据库不可用时必须返回 503，而不是继续声称健康。"""
    database_path = tmp_path / "closed.duckdb"
    database = open_lyra_database(str(database_path))
    application = create_app(_settings(database_path), database)
    database.close()

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.get("/v1/health")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["database"] == {"status": "unavailable"}


def test_system_info_exposes_protocol_two(client: TestClient) -> None:
    """协议元数据必须与 TypeScript 版一致：协议 2、区间 2–2。"""
    response = client.get("/v1/system/info")

    assert response.status_code == 200
    assert response.json() == {
        "server_version": "0.1.0",
        "protocol_version": 2,
        "min_supported_protocol_version": 2,
        "max_supported_protocol_version": 2,
        "schema_version": CURRENT_SCHEMA_VERSION,
    }


def test_system_info_reports_the_compiled_schema_version(client) -> None:
    """**``/v1/system/info`` 的 ``schema_version`` 与数据库状态无关。**

    它此前从健康快照读取、数据库不可用时回退为 **0**——而客户端正是用这个端点判断
    协议与模式兼容性。``0`` 不是任何有效的模式版本，会让客户端误判（例如认为服务端
    模式过旧而拒绝连接），而真实原因只是**数据库暂时不可用**。

    参考实现用编译期常量，因此这里断言取值等于常量本身。
    """
    from lyra.db.migrations import CURRENT_SCHEMA_VERSION

    body = client.get("/v1/system/info").json()

    assert body["schema_version"] == CURRENT_SCHEMA_VERSION == 15


def test_system_info_reports_the_protocol_window(client) -> None:
    """协议版本区间与参考实现一致（``2`` / ``2`` / ``2``）。"""
    body = client.get("/v1/system/info").json()

    assert body["protocol_version"] == 2
    assert body["min_supported_protocol_version"] == 2
    assert body["max_supported_protocol_version"] == 2
    assert body["server_version"] == "0.1.0"


def test_system_info_exposes_exactly_the_five_documented_keys(client) -> None:
    """字段集合是闭集——**多一个字段就是协议变更**。

    ``/v1/system/info`` 的字段用 snake_case（与 ``request_id`` 同类：协议级元数据，
    不是领域数据），因此不受"公开字段一律 camelCase"的约定约束。
    """
    body = client.get("/v1/system/info").json()

    assert set(body) == {
        "server_version", "protocol_version", "min_supported_protocol_version",
        "max_supported_protocol_version", "schema_version",
    }
