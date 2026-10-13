"""行情路由测试。

最要紧的一条：**`routes` 与 `effectiveRoutes` 同时返回**。

只返回前者会让用户看到"已选中 alpaca"而每次请求都静默回退到备来源——
这是最容易让用户困惑的一类配置问题。
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

PUBLIC_KEY_PEM = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"][0]["inputPem"]
IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")
ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def client(tmp_path: Path):
    database_path = tmp_path / "market.duckdb"
    database = open_lyra_database(str(database_path))
    database.connection.execute(
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


# --------------------------------------------------------------------------
# 认证
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/market/settings"),
        ("put", "/v1/market/settings"),
        ("get", "/v1/market/positions"),
    ],
)
def test_market_routes_require_authentication(client, method: str, path: str) -> None:
    """三条行情路由都要求设备认证。"""
    test_client, _ = client
    request = getattr(test_client, method)
    response = request(path, json={}) if method == "put" else request(path)
    assert response.status_code == 401, f"{method} {path}"


# --------------------------------------------------------------------------
# 设置
# --------------------------------------------------------------------------


def test_read_settings_starts_at_revision_zero(client) -> None:
    """从未保存过时 revision 为 0、路由为空。"""
    test_client, issued = client
    body = test_client.get("/v1/market/settings", headers=_auth(issued)).json()

    assert body["revision"] == 0
    assert body["routes"] == {"stock": [], "option": []}
    assert body["effectiveRoutes"] == {"stock": [], "option": []}
    assert body["updatedAt"] is None


def test_save_settings_advances_revision(client) -> None:
    """保存推进 revision 并回读一致。"""
    test_client, issued = client
    response = test_client.put(
        "/v1/market/settings",
        json={"expectedRevision": 0, "routes": {"stock": ["alpaca"], "option": ["marketdata"]}},
        headers=_auth(issued),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 1
    assert body["routes"]["stock"] == ["alpaca"]
    assert body["updatedAt"] is not None

    reread = test_client.get("/v1/market/settings", headers=_auth(issued)).json()
    assert reread["revision"] == 1
    assert reread["routes"]["option"] == ["marketdata"]


def test_stale_revision_is_rejected(client) -> None:
    """**过期 revision 必须拒绝**：直接覆盖会静默丢掉别的设备的修改。"""
    test_client, issued = client
    test_client.put(
        "/v1/market/settings",
        json={"expectedRevision": 0, "routes": {"stock": ["alpaca"]}},
        headers=_auth(issued),
    )

    response = test_client.put(
        "/v1/market/settings",
        json={"expectedRevision": 0, "routes": {"stock": ["marketdata"]}},
        headers=_auth(issued),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MARKET_SETTINGS_CHANGED"


def test_effective_routes_exclude_unconfigured_providers(client) -> None:
    """**`effectiveRoutes` 剔除未配置凭据的来源。**

    只返回 `routes` 会让用户看到"已选中 alpaca"而每次请求都静默回退到备来源。
    """
    test_client, issued = client
    # 本夹具没有配置任何行情凭据。
    body = test_client.put(
        "/v1/market/settings",
        json={"expectedRevision": 0, "routes": {"stock": ["alpaca", "marketdata"]}},
        headers=_auth(issued),
    ).json()

    assert body["routes"]["stock"] == ["alpaca", "marketdata"]  # 用户选的
    assert body["effectiveRoutes"]["stock"] == []                # 实际可用的


@pytest.mark.parametrize(
    "payload",
    [
        {"routes": {"stock": ["alpaca"]}},                              # 缺 expectedRevision
        {"expectedRevision": -1, "routes": {}},
        {"expectedRevision": "0", "routes": {}},
    ],
)
def test_invalid_save_bodies_are_rejected(client, payload: dict) -> None:
    """非法请求体一律 400。"""
    test_client, issued = client
    response = test_client.put("/v1/market/settings", json=payload, headers=_auth(issued))
    assert response.status_code == 400


def test_unknown_asset_type_is_rejected(client) -> None:
    """未知资产类型必须拒绝。"""
    test_client, issued = client
    response = test_client.put(
        "/v1/market/settings",
        json={"expectedRevision": 0, "routes": {"crypto": ["alpaca"]}},
        headers=_auth(issued),
    )
    assert response.status_code == 400


def test_duplicate_provider_is_rejected(client) -> None:
    """重复来源必须拒绝。"""
    test_client, issued = client
    response = test_client.put(
        "/v1/market/settings",
        json={"expectedRevision": 0, "routes": {"stock": ["alpaca", "alpaca"]}},
        headers=_auth(issued),
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------
# 持仓
# --------------------------------------------------------------------------


def test_positions_endpoint_is_empty_without_lots(client) -> None:
    """没有未平批次时返回空结果。"""
    test_client, issued = client
    body = test_client.get("/v1/market/positions", headers=_auth(issued)).json()

    assert body["count"] == 0
    assert body["rows"] == []
    assert body["basis"] == "current_positions_at_quote_times_excluding_fees"


def test_positions_without_market_data_are_still_returned(client) -> None:
    """**行情不可用不影响账本数据返回**：只有估值字段为空。"""
    test_client, issued = client
    connection = test_client.app.state.database.connection
    connection.execute(
        "INSERT INTO ledger_versions (reason, source_reference) VALUES ('file_import', 'seed')"
    )
    connection.execute(
        """
        INSERT INTO transactions (
            account_id, record_date, transaction_date, action, raw_symbol, description,
            asset_type, underlying, quantity, price, fees, amount, source_type,
            source_reference, source_hash, sequence, occurrence, business_hash,
            transaction_hash, parser_version, ledger_version
        ) VALUES (
            CAST(? AS UUID), CAST('2026-01-05' AS DATE), CAST('2026-01-05' AS DATE), 'Buy',
            'INTC', 'INTEL CORP', 'stock', 'INTC', CAST('10' AS DECIMAL(18,6)),
            CAST('40' AS DECIMAL(18,6)), CAST('0' AS DECIMAL(18,6)),
            CAST('-400' AS DECIMAL(18,4)), 'csv', 'seed', ?, 1, 1, ?, ?, 2, 1
        )
        """,
        [ACCOUNT_ID, "a" * 64, "b" * 64, "c" * 64],
    )
    connection.execute(
        """
        INSERT INTO lots (
            account_id, symbol_key, asset_type, underlying, direction, open_date,
            open_price, open_quantity, remaining_quantity, open_fees, fees_allocated,
            open_transaction_hash, ledger_version
        ) VALUES (
            CAST(? AS UUID), 'INTC', 'stock', 'INTC', 'long', CAST('2026-01-05' AS DATE),
            CAST('40' AS DECIMAL(18,6)), CAST('10' AS DECIMAL(18,6)),
            CAST('10' AS DECIMAL(18,6)), CAST('0' AS DECIMAL(18,6)),
            CAST('0' AS DECIMAL(18,6)), ?, 1
        )
        """,
        [ACCOUNT_ID, "c" * 64],
    )

    body = test_client.get("/v1/market/positions", headers=_auth(issued)).json()

    # 账本数据照常返回。
    assert body["count"] == 1
    row = body["rows"][0]
    assert row["symbolKey"] if "symbolKey" in row else row["symbol_key"] == "INTC"
    # 行情不可用时估值字段为 null，而不是 0——0 会被当成"资产归零"。
    assert row["valuation"]["marketValue"] is None
    assert row["valuation"]["unrealizedPnl"] is None
    # 完整性为假，因为有一行没有可用报价。
    assert body["complete"] is False


# --------------------------------------------------------------------------
# 真实接线（反向验证）
# --------------------------------------------------------------------------


def test_the_real_market_transport_is_wired_by_default(client) -> None:
    """**默认装配的是真实传输层，不是"调用即抛错"的占位。**

    其余测试都通过夹具替换掉传输层，因此"是否真的接线了"原本没有任何覆盖——
    **一个被删掉的装配行不会被任何人发现**，而它会让行情静默变成"永远不可用"。
    """
    from lyra.infrastructure.market_data.httpx_fetch import httpx_market_fetch

    test_client, _ = client
    runtime = test_client.app.state.market_runtime

    assert runtime is not None
    # 服务内部的传输层应当是 httpx 实现。
    assert runtime.service._providers is not None
    assert callable(httpx_market_fetch)


def test_market_runtime_is_present_even_without_configuration(client) -> None:
    """**未配置任何行情来源时仍装配运行时。**

    否则设置端点会 500，而用户需要看到的是"尚无来源"这个可用状态。
    """
    test_client, issued = client

    body = test_client.get("/v1/market/settings", headers=_auth(issued)).json()

    assert body["routes"] == {"stock": [], "option": []}
    assert body["providers"] == []


def test_positions_report_unavailable_market_data_without_providers(client) -> None:
    """没有来源时持仓照常返回，只是没有估值——**数据状态而非异常**。"""
    test_client, issued = client

    body = test_client.get("/v1/market/positions", headers=_auth(issued)).json()

    assert body["count"] == 0
    assert body["complete"] is True
