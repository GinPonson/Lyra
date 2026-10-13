"""禁止缓存的响应覆盖。

这些响应含**凭据或供应商密钥配置**，被缓存等于让它们在预期生命周期之外留存：

| 路径 | 内容 |
| --- | --- |
| ``/v1/auth/*`` | state 与授权地址 |
| ``POST /v1/devices/tokens`` | **访问令牌与刷新令牌** |
| ``/v1/market/settings`` | 行情提供方的 **API 密钥配置** |
| ``POST /v1/ledger/accounts`` | 创建账户的响应 |

参考实现的覆盖是**两层**：一个 ``/v1/auth/`` 前缀的全局钩子 + 几处逐路由自设。
**只实现前者会漏掉后三者**——此前正是如此：auth 有 ``no-store``，另三组没有，
而"auth 有"让人以为这一项已覆盖。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.protocol import CLIENT_PROTOCOL_HEADERS
from lyra.application.device_pairing_service import DevicePairingService
from lyra.application.write_coordinator import WriteCoordinator
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.main import create_app

_KEYS = Path(__file__).parent / "fixtures" / "device-keys.json"
_PUBLIC_KEY_PEM = json.loads(_KEYS.read_text(encoding="utf-8"))["cases"][0]["inputPem"]
_IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")


@pytest.fixture()
def client(tmp_path: Path):
    """构造应用与一个有效访问令牌。

    **不预置账本数据**：本文件只验证响应头与正文上限。
    """
    database_path = tmp_path / "nostore.duckdb"
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
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
            yield _Authed(test_client, issued)
    finally:
        database.close()


class _Authed:
    """带认证头的轻量包装。

    **默认注入认证头**：本文件关心的是响应头与正文上限，不是鉴权；若不带令牌，
    响应会因 401 而根本不经过业务处理器。
    """

    def __init__(self, client: TestClient, issued: object) -> None:
        self._client = client
        self._issued = issued

    def request(self, method: str, path: str, **kwargs):
        headers = {**kwargs.pop("headers", {}),
                   "Authorization": f"Bearer {self._issued.access_token}"}
        return self._client.request(method, path, headers=headers, **kwargs)


def _cache_control(client, method: str, path: str, **kwargs) -> str | None:
    """取某响应的 ``cache-control``。"""
    return client.request(method, path, **kwargs).headers.get("cache-control")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/auth/providers"),
        ("GET", "/v1/market/settings"),
        ("POST", "/v1/devices/tokens"),
        ("POST", "/v1/ledger/accounts"),
    ],
)
def test_credential_bearing_responses_are_not_cacheable(client, method: str, path: str) -> None:
    """**含凭据或密钥配置的响应必须带 ``cache-control: no-store``。**"""
    assert _cache_control(client, method, path, json={}) == "no-store", path


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/health"),
        ("GET", "/v1/system/info"),
        ("GET", "/v1/ledger/summary"),
    ],
)
def test_ordinary_responses_are_left_alone(client, method: str, path: str) -> None:
    """**对照**：普通响应不应被加上 ``no-store``。

    只断言"该有的有"是不够的——如果实现给**每个**响应都加 ``no-store``，上一条同样
    通过，而那会禁掉所有客户端缓存、把只读端点的性能特征整个改掉。
    """
    assert _cache_control(client, method, path, json={}) is None, path


def test_market_settings_body_is_capped(client) -> None:
    """**行情设置正文上限 2048 字节**（与参考实现的 ``bodyLimit`` 一致）。

    该正文只含一个修订号与两组路由；允许超长等于允许把任意大的内容送进校验与日志。
    """
    response = client.request(
        "PUT", "/v1/market/settings",
        headers={"content-length": "4096"},
        content=b"x" * 4096,
    )

    assert response.status_code == 400
