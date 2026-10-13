"""健康端点的 Agent 段。

**``availableProviders`` 曾是一个写死的常量**（``1 if configured else 0``）：模块文档
说"各段缺失时不填占位值，健康端点谎报可用会让故障排查建立在假信息上"，而这一行恰好
违反了它——提供方不可用时仍报 ``available`` + 0 个不可用，排查者据此会以为对话能力
是好的。

参考实现的语义（``createServiceHealthSnapshot``）：

```
agentAvailability === null   → status="unknown"，两个计数都是 0
unavailableProviders > 0     → status="degraded"
否则                          → status="available"
任何非 available             → 整体 degraded
```

因此这里从**真实 provider 列表**推导，并覆盖"部分提供方不可用"的场景。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lyra.api.health import create_health_router
from lyra.application.agent_availability import AgentAvailabilitySnapshot
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.protocol import CLIENT_PROTOCOL_HEADERS
from lyra.main import create_app


@pytest.fixture()
def database(tmp_path: Path):
    """一个已迁移的空账本。"""
    connection = open_lyra_database(str(tmp_path / "health.duckdb"))
    try:
        yield connection
    finally:
        connection.close()


def _snapshot(*statuses: str) -> AgentAvailabilitySnapshot:
    """构造含多个 provider 的可用性快照（真实快照允许任意多条）。"""
    return AgentAvailabilitySnapshot(
        checked_at="2026-01-01T00:00:00.000Z",
        providers=tuple(
            {"provider": f"provider-{index}", "status": status}
            for index, status in enumerate(statuses)
        ),
    )


def _health(database, availability: object) -> dict:
    """用注入的可用性来源跑一次 ``GET /v1/health``。"""
    application = FastAPI()
    application.include_router(
        create_health_router(database, agent_availability=lambda: availability)
    )
    with TestClient(application) as client:
        response = client.get("/v1/health")

    assert response.status_code == 200
    return response.json()


def test_a_partly_unavailable_provider_set_is_degraded(database) -> None:
    """**有提供方不可用时 Agent 段是 ``degraded``，且计数来自真实列表。**

    写死的计数会报 ``available``/1/0——把"一半依赖坏了"报成完全可用，正是健康端点
    最不该做的事。
    """
    body = _health(database, _snapshot("available", "unavailable"))

    assert body["agents"] == {
        "status": "degraded",
        "availableProviders": 1,
        "unavailableProviders": 1,
    }
    assert body["status"] == "degraded"


def test_a_single_unavailable_provider_is_degraded(database) -> None:
    """**唯一一个提供方不可用 → ``degraded``**，而不是靠"是否配置过"反推。"""
    body = _health(database, _snapshot("unavailable"))

    assert body["agents"]["status"] == "degraded"
    assert body["agents"]["availableProviders"] == 0
    assert body["agents"]["unavailableProviders"] == 1
    assert body["status"] == "degraded"


def test_all_providers_available_keeps_agents_available(database) -> None:
    """**对照**：全部可用时才是 ``available``（两个计数仍来自真实列表）。

    只测"坏了要降级"不够——若实现永远返回 degraded，那些测试同样通过，而健康端点就
    再也报不出"健康"了。
    """
    body = _health(database, _snapshot("available"))

    assert body["agents"] == {
        "status": "available",
        "availableProviders": 1,
        "unavailableProviders": 0,
    }
    assert body["status"] == "healthy"


def test_a_missing_runtime_is_unknown_with_zero_counts(database) -> None:
    """**运行时缺席（快照为 ``None``）→ ``unknown``，两个计数都是 0。**

    "不知道是否可用"与"知道它坏了"是不同的信息：前者不该被当成有一个不可用的提供方。
    但两者都让整体降级——判定是 ``status !== "available"``。
    """
    body = _health(database, None)

    assert body["agents"] == {
        "status": "unknown",
        "availableProviders": 0,
        "unavailableProviders": 0,
    }
    assert body["status"] == "degraded"


def test_the_application_wires_the_real_availability_snapshot(tmp_path: Path) -> None:
    """**应用装配的是真实快照，不是布尔。**

    测试环境未配置模型：参考实现里运行时的可用性快照仍会给出**一条** ``unavailable``
    的 provider（``not_configured``），因此健康端点是 ``degraded`` + 0/1——而不是
    "unknown + 0/0"。这也正是旧写死实现与参考实现的可见差异。
    """
    database_path = tmp_path / "app-health.duckdb"
    database = open_lyra_database(str(database_path))
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    application = create_app(settings, database)
    try:
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
            agents = client.get("/v1/health").json()["agents"]
    finally:
        database.close()

    assert agents == {
        "status": "degraded",
        "availableProviders": 0,
        "unavailableProviders": 1,
    }
