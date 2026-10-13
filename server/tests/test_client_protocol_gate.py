"""客户端协议门。

**它是一处曾经整块缺失的协议面。** 迁移期的 TS 部署探针断言"缺少
``x-lyra-protocol-version`` 时返回 426"，而当时的 Python 实现**完全不检查这个头**——
缺失时只会落到 401（未认证），客户端据此会以为是凭据问题。

**426 与 401 的区别对客户端是有意义的**：

- **401** 说"你的凭据不对" → 客户端会去刷新令牌、重新登录；
- **426** 说"你的 App 版本与服务端不兼容" → 客户端唯一正确的动作是**提示用户升级**。

报 401 会让用户在一个无法靠登录解决的问题上反复尝试。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.application.device_pairing_service import DevicePairingService
from lyra.application.write_coordinator import WriteCoordinator
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.main import create_app
from lyra.protocol import CLIENT_PROTOCOL_HEADERS

_KEYS = Path(__file__).parent / "fixtures" / "device-keys.json"
_PUBLIC_KEY_PEM = json.loads(_KEYS.read_text(encoding="utf-8"))["cases"][0]["inputPem"]
_IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")


@pytest.fixture()
def application(tmp_path: Path):
    """构造应用（**TestClient 不带默认协议头**——本文件要测的正是它的缺失）。"""
    database_path = tmp_path / "gate.duckdb"
    database = open_lyra_database(str(database_path))
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    application = create_app(settings, database)
    repository = DuckDbDeviceRepository(database.connection)
    service = DevicePairingService(repository, WriteCoordinator())
    issued = asyncio.run(service.authorize_owner_device(
        identity=_IDENTITY, display_name="Pixel 9", public_key_pem=_PUBLIC_KEY_PEM
    ))
    try:
        yield application, issued
    finally:
        database.close()


def test_missing_protocol_header_is_426_not_401(application) -> None:
    """**缺失协议头返回 426，而不是 401。**

    这是那条差异的核心：426 让客户端知道该升级，401 会让它去刷新令牌——而升级 App 与
    刷新令牌是两件完全不同的事。
    """
    app, issued = application
    with TestClient(app) as client:
        response = client.get(
            "/v1/ledger/accounts",
            headers={"Authorization": f"Bearer {issued.access_token}"},
        )

    assert response.status_code == 426
    assert response.json()["code"] == "CLIENT_VERSION_INCOMPATIBLE"


def test_the_426_body_carries_the_supported_range(application) -> None:
    """426 的 ``details`` 给出**服务端支持的版本区间**。

    没有区间，客户端只知道"不兼容"而不知道该升到哪一版。
    """
    app, issued = application
    with TestClient(app) as client:
        body = client.get("/v1/ledger/accounts").json()

    assert body["details"] == {
        "min_supported_protocol_version": 2,
        "max_supported_protocol_version": 2,
    }
    assert body["retryable"] is False


@pytest.mark.parametrize("raw", ["", "abc", "1", "999", "-1", "2.5"])
def test_out_of_range_versions_are_rejected(application, raw: str) -> None:
    """非整数或超出区间的版本一律 426。"""
    app, _ = application
    with TestClient(app, headers={"x-lyra-protocol-version": raw}) as client:
        response = client.get("/v1/ledger/accounts")

    assert response.status_code == 426, raw


def test_a_supported_version_passes(application) -> None:
    """**对照**：受支持的版本必须放行。

    只断言"非法被拒"是不够的——若实现把所有请求都判为 426，那条测试同样通过，
    而客户端将永远无法访问任何端点。
    """
    app, issued = application
    with TestClient(app, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.get(
            "/v1/ledger/accounts",
            headers={"Authorization": f"Bearer {issued.access_token}"},
        )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/v1/health",
        "/v1/system/info",
        "/v1/auth/providers",
    ],
)
def test_public_routes_do_not_require_the_header(application, path: str) -> None:
    """**公开路由不要求协议头。**

    它们的调用方发不出这个头：健康探针、尚未得知服务端能力的客户端启动流程。
    要求它们带头会让服务在"客户端还不知道服务端支持什么版本"时**无法被探活**。
    """
    app, _ = application
    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 200


def test_the_gate_runs_before_authentication(application) -> None:
    """**协议门先于认证**。

    一个版本不兼容的客户端若先撞上 401，会去刷新令牌——而它真正需要的是升级 App。
    先认证后校验版本会让客户端拿到误导性的错误。
    """
    app, _ = application
    with TestClient(app) as client:
        response = client.get("/v1/ledger/accounts")

    assert response.status_code == 426
