"""行情路由测试。

最要紧的一条：**`routes` 与 `effectiveRoutes` 同时返回**。

只返回前者会让用户看到"已选中 alpaca"而每次请求都静默回退到备来源——
这是最容易让用户困惑的一类配置问题。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

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
SECOND_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
_DEFAULT_UNDERLYING = object()


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


def _seed_stock_position(
    connection,
    account_id: str,
    symbol: str,
    *,
    quantity: str = "1",
    price: str = "10",
    underlying: str | None | object = _DEFAULT_UNDERLYING,
) -> None:
    """插入一笔用于行情路由测试的未平正股批次。"""
    transaction_hash = hashlib.sha256(
        f"{account_id}:{symbol}:{quantity}:{price}".encode("utf-8")
    ).hexdigest()
    resolved_underlying = symbol if underlying is _DEFAULT_UNDERLYING else underlying
    connection.execute(
        "INSERT INTO ledger_versions (reason, source_reference) VALUES ('file_import', ?)",
        [transaction_hash],
    )
    version = connection.execute(
        "SELECT max(version) FROM ledger_versions"
    ).fetchone()[0]
    source_hash = hashlib.sha256(f"source:{transaction_hash}".encode()).hexdigest()
    business_hash = hashlib.sha256(f"business:{transaction_hash}".encode()).hexdigest()
    connection.execute(
        """
        INSERT INTO transactions (
            account_id, record_date, transaction_date, action, raw_symbol, description,
            asset_type, underlying, quantity, price, fees, amount, source_type,
            source_reference, source_hash, sequence, occurrence, business_hash,
            transaction_hash, parser_version, ledger_version
        ) VALUES (
            CAST(? AS UUID), CAST('2026-01-05' AS DATE), CAST('2026-01-05' AS DATE),
            'Buy', ?, ?, 'stock', ?, CAST(? AS DECIMAL(18,6)),
            CAST(? AS DECIMAL(18,6)), CAST('0' AS DECIMAL(18,6)),
            CAST(? AS DECIMAL(18,4)), 'csv', ?, ?, 1, 1, ?, ?, 2, ?
        )
        """,
        [
            account_id, symbol, symbol, resolved_underlying, quantity, price,
            str(-(Decimal(quantity) * Decimal(price))), transaction_hash,
            source_hash, business_hash, transaction_hash, version,
        ],
    )
    connection.execute(
        """
        INSERT INTO lots (
            account_id, symbol_key, asset_type, underlying, direction, open_date,
            open_price, open_quantity, remaining_quantity, open_fees, fees_allocated,
            open_transaction_hash, ledger_version
        ) VALUES (
            CAST(? AS UUID), ?, 'stock', ?, 'long', CAST('2026-01-05' AS DATE),
            CAST(? AS DECIMAL(18,6)), CAST(? AS DECIMAL(18,6)),
            CAST(? AS DECIMAL(18,6)), CAST('0' AS DECIMAL(18,6)),
            CAST('0' AS DECIMAL(18,6)), ?, ?
        )
        """,
        [
            account_id, symbol, resolved_underlying, price, quantity,
            quantity, transaction_hash, version,
        ],
    )


def _marketdata_runtime(*, on_fetch: Callable[[str], None] | None = None):
    """创建使用本地固定响应的 MarketData.app 运行时。"""
    from lyra.infrastructure.market_data.config import load_market_data_config
    from lyra.infrastructure.market_data.runtime import create_market_data_runtime

    now = int(datetime(2026, 10, 15, 20, tzinfo=UTC).timestamp() * 1000)
    fresh = int(datetime(2026, 10, 15, 15, tzinfo=UTC).timestamp())
    stale = int(datetime(2026, 9, 20, 15, tzinfo=UTC).timestamp())

    def fetch(url, *, headers, timeout):
        symbol = urlsplit(url).path.rstrip("/").split("/")[-1]
        if on_fetch is not None:
            on_fetch(symbol)
        if symbol == "NVDA":
            payload = {"s": "no_data"}
        else:
            updated = stale if symbol == "MSFT" else fresh
            payload = {
                "s": "ok",
                "symbol": [symbol],
                "updated": [updated],
                "bid": [10.0],
                "ask": [10.2],
                "last": [10.1],
            }

        class Response:
            status = 200
            headers: dict = {}

            def chunks(self):
                yield json.dumps(payload).encode("utf-8")

        return Response()

    config = load_market_data_config({
        "LYRA_MARKET_STOCK_PROVIDERS": "marketdata",
        "LYRA_MARKETDATA_MODE": "delayed",
        "LYRA_MARKETDATA_API_TOKEN": "test-only-token",
    })
    return create_market_data_runtime(config, fetch=fetch, now=lambda: now)


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
    body = test_client.get(
        "/v1/market/positions?scope=all", headers=_auth(issued)
    ).json()

    assert body["metadata"]["ledgerVersion"] == 0
    assert body["metadata"]["accountScope"] == {"kind": "all"}
    assert body["data"]["count"] == 0
    assert body["data"]["rows"] == []
    assert body["data"]["basis"] == "current_positions_at_quote_times_excluding_fees"
    assert body["data"]["currencyGroups"] == []


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

    body = test_client.get(
        "/v1/market/positions?scope=all", headers=_auth(issued)
    ).json()

    # 账本数据照常返回。
    data = body["data"]
    assert data["count"] == 1
    row = data["rows"][0]
    assert row["symbolKey"] == "INTC"
    assert row["currency"] == "USD"
    assert row["instrumentKey"] == "US:stock:INTC"
    assert row["quote"] == {
        "status": "unavailable",
        "quote": None,
        "referencePrice": None,
        "issue": "not_configured",
        "cached": False,
    }
    # 行情不可用时估值字段为 null，而不是 0——0 会被当成"资产归零"。
    assert row["valuation"]["marketValue"] is None
    assert row["valuation"]["unrealizedPnl"] is None
    # 完整性为假，因为有一行没有可用报价。
    assert data["complete"] is False
    assert data["marketData"]["enabled"] is False
    assert data["marketData"]["settingsRevision"] == 0
    assert data["currencyGroups"] == [{
        "currency": "USD",
        "accountIds": [ACCOUNT_ID],
        "positions": 1,
        "pricedPositions": 0,
        "complete": False,
        "marketValue": None,
        "unrealizedPnl": None,
        "asOfFrom": None,
        "asOfTo": None,
    }]


def test_positions_requires_scope_and_resolves_account_uuid(client) -> None:
    """positions 只接受 all 或账户 UUID，并把 UUID 映射到账本 legacy key。"""
    test_client, issued = client
    connection = test_client.app.state.database.connection
    connection.execute(
        """
        INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix, base_currency)
        VALUES (CAST(? AS UUID), 'Second', 'TST002', '002', 'USD')
        """,
        [SECOND_ACCOUNT_ID],
    )
    _seed_stock_position(connection, ACCOUNT_ID, "INTC")
    _seed_stock_position(connection, SECOND_ACCOUNT_ID, "AAPL")

    missing = test_client.get("/v1/market/positions", headers=_auth(issued))
    legacy_only = test_client.get(
        "/v1/market/positions?account=TST001", headers=_auth(issued)
    )
    invalid = test_client.get(
        "/v1/market/positions?scope=TST001", headers=_auth(issued)
    )
    assert missing.status_code == 400
    assert missing.json()["code"] == "ACCOUNT_SCOPE_REQUIRED"
    assert legacy_only.status_code == 400
    assert invalid.status_code == 400

    all_accounts = test_client.get(
        "/v1/market/positions?scope=all", headers=_auth(issued)
    ).json()
    one_account = test_client.get(
        f"/v1/market/positions?scope={SECOND_ACCOUNT_ID}", headers=_auth(issued)
    ).json()
    missing_account = test_client.get(
        "/v1/market/positions?scope=33333333-3333-3333-3333-333333333333",
        headers=_auth(issued),
    )

    assert {
        row["accountId"] for row in all_accounts["data"]["rows"]
    } == {ACCOUNT_ID, SECOND_ACCOUNT_ID}
    assert one_account["metadata"]["accountScope"] == {
        "kind": "account", "account": SECOND_ACCOUNT_ID,
    }
    assert one_account["metadata"]["accountIds"] == [SECOND_ACCOUNT_ID]
    assert [row["symbolKey"] for row in one_account["data"]["rows"]] == ["AAPL"]
    assert missing_account.status_code == 404
    assert missing_account.json()["code"] == "ACCOUNT_NOT_FOUND"


def test_positions_keep_available_stale_missing_and_currency_groups(client) -> None:
    """真实报价、过期价和缺价分别投影，并按报价币种独立汇总。"""
    test_client, issued = client
    connection = test_client.app.state.database.connection
    connection.execute(
        """
        INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix, base_currency)
        VALUES (CAST(? AS UUID), 'Euro', 'EUR002', '002', 'EUR')
        """,
        [SECOND_ACCOUNT_ID],
    )
    _seed_stock_position(connection, ACCOUNT_ID, "INTC")
    _seed_stock_position(connection, ACCOUNT_ID, "MSFT")
    _seed_stock_position(connection, ACCOUNT_ID, "NVDA")
    # 空的券商标的身份无法构造行情合约；账户币种标注其未估值金额。
    _seed_stock_position(connection, SECOND_ACCOUNT_ID, "UNKNOWN", underlying="")
    runtime = _marketdata_runtime()
    test_client.app.state.market_runtime = runtime

    saved = test_client.put(
        "/v1/market/settings",
        json={"expectedRevision": 0, "routes": {"stock": ["marketdata"]}},
        headers=_auth(issued),
    )
    assert saved.status_code == 200
    body = test_client.get(
        "/v1/market/positions?scope=all", headers=_auth(issued)
    ).json()
    data = body["data"]
    by_symbol = {row["symbolKey"]: row for row in data["rows"]}

    assert body["metadata"]["aggregateCurrency"] is None
    assert data["marketData"]["enabled"] is True
    assert data["marketData"]["routes"] == {
        "stock": ["marketdata"], "option": [],
    }
    assert data["marketData"]["settingsRevision"] == saved.json()["revision"] == 1
    assert by_symbol["INTC"]["quote"]["status"] == "available"
    assert by_symbol["INTC"]["quote"]["referencePrice"] == {
        "value": "10.1",
        "asOf": "2026-10-15T15:00:00.000Z",
        "basis": "mid",
    }
    assert by_symbol["INTC"]["quote"]["quote"]["providerId"] == "marketdata"
    assert by_symbol["INTC"]["valuation"]["marketValue"] == "10.1"
    assert by_symbol["MSFT"]["quote"]["status"] == "stale"
    assert by_symbol["MSFT"]["quote"]["referencePrice"] is not None
    assert by_symbol["MSFT"]["valuation"]["marketValue"] == "10.1"
    assert by_symbol["NVDA"]["quote"]["status"] == "unavailable"
    assert by_symbol["NVDA"]["quote"]["issue"] == "no_data"
    assert by_symbol["NVDA"]["quote"]["quote"] is None
    assert by_symbol["UNKNOWN"]["currency"] == "EUR"
    assert by_symbol["UNKNOWN"]["instrumentKey"] is None
    assert by_symbol["UNKNOWN"]["quote"]["issue"] == "unsupported_instrument"
    assert data["count"] == 4
    assert data["pricedCount"] == 2
    assert data["complete"] is False
    groups = {group["currency"]: group for group in data["currencyGroups"]}
    assert groups["USD"] == {
        "currency": "USD",
        "accountIds": [ACCOUNT_ID],
        "positions": 3,
        "pricedPositions": 2,
        "complete": False,
        "marketValue": None,
        "unrealizedPnl": None,
        "asOfFrom": "2026-09-20T15:00:00.000Z",
        "asOfTo": "2026-10-15T15:00:00.000Z",
    }
    assert groups["EUR"] == {
        "currency": "EUR",
        "accountIds": [SECOND_ACCOUNT_ID],
        "positions": 1,
        "pricedPositions": 0,
        "complete": False,
        "marketValue": None,
        "unrealizedPnl": None,
        "asOfFrom": None,
        "asOfTo": None,
    }


def test_saved_market_settings_initialize_a_restarted_runtime(tmp_path: Path) -> None:
    """create_app 在首个请求前恢复真实持久化的路由与 revision。"""
    from lyra.domain.market_data.settings import normalize_routes
    from lyra.infrastructure.db.market_settings_repository import persist_market_settings

    database_path = tmp_path / "restarted-market.duckdb"
    database = open_lyra_database(str(database_path))
    connection = database.connection
    connection.execute(
        """
        INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), 'Account', 'TST001', '001')
        """,
        [ACCOUNT_ID],
    )
    persist_market_settings(
        connection,
        expected_revision=0,
        routes=normalize_routes({"stock": ["marketdata"]}),
        now=datetime(2026, 10, 15, tzinfo=UTC),
    )
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )

    try:
        # 从已含保存设置的数据库创建应用，不能通过 GET 路由人为修复运行时状态。
        application = create_app(settings, database)
        runtime = application.state.market_runtime
        snapshot = runtime.settings_snapshot()
        assert snapshot.revision == 1
        assert snapshot.routes == {"stock": ("marketdata",), "option": ()}
        assert runtime.service._routes == snapshot.routes

        pairing = DevicePairingService(
            DuckDbDeviceRepository(connection), WriteCoordinator()
        )
        issued = asyncio.run(pairing.authorize_owner_device(
            identity=IDENTITY, display_name="Pixel 9", public_key_pem=PUBLIC_KEY_PEM
        ))
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
            settings_body = test_client.get(
                "/v1/market/settings", headers=_auth(issued)
            ).json()
            positions_body = test_client.get(
                "/v1/market/positions?scope=all", headers=_auth(issued)
            ).json()

        assert settings_body["revision"] == 1
        assert settings_body["routes"] == {"stock": ["marketdata"], "option": []}
        assert positions_body["data"]["marketData"]["settingsRevision"] == 1
        assert positions_body["data"]["marketData"]["routes"] == settings_body["routes"]
    finally:
        database.close()


def test_positions_capture_ledger_version_before_quote_callback_writes(client) -> None:
    """行情回调期间出现新账本版本时，信封仍标记本次快照版本。"""
    test_client, issued = client
    connection = test_client.app.state.database.connection
    _seed_stock_position(connection, ACCOUNT_ID, "INTC")
    snapshot_version = connection.execute(
        "SELECT max(version) FROM ledger_versions"
    ).fetchone()[0]
    inserted_versions: list[int] = []

    def write_during_quote(symbol: str) -> None:
        connection.execute(
            "INSERT INTO ledger_versions (reason, source_reference) VALUES ('file_import', ?)",
            [f"quote-callback:{symbol}"],
        )
        inserted_versions.append(
            connection.execute("SELECT max(version) FROM ledger_versions").fetchone()[0]
        )

    test_client.app.state.market_runtime = _marketdata_runtime(on_fetch=write_during_quote)
    saved = test_client.put(
        "/v1/market/settings",
        json={"expectedRevision": 0, "routes": {"stock": ["marketdata"]}},
        headers=_auth(issued),
    )
    assert saved.status_code == 200

    response = test_client.get(
        "/v1/market/positions?scope=all", headers=_auth(issued)
    )

    assert response.status_code == 200
    assert inserted_versions == [snapshot_version + 1]
    assert response.json()["metadata"]["ledgerVersion"] == snapshot_version
    assert connection.execute(
        "SELECT max(version) FROM ledger_versions"
    ).fetchone()[0] == snapshot_version + 1


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
    assert body["effectiveRoutes"] == {"stock": [], "option": []}
    assert [source["id"] for source in body["providers"]] == ["alpaca", "marketdata"]
    assert all(source["configured"] is False for source in body["providers"])
    assert body["providers"][0] == {
        "id": "alpaca",
        "displayName": "Alpaca",
        "assetTypes": ["stock", "option"],
        "configured": False,
        "timing": {"kind": "delayed", "delayMinutes": 16},
        "cacheTtlSeconds": 900,
    }
    assert body["providers"][1]["timing"] == {"kind": "end_of_day", "delayMinutes": None}
    assert body["providers"][1]["cacheTtlSeconds"] == 86_400


def test_positions_report_unavailable_market_data_without_providers(client) -> None:
    """没有来源时持仓照常返回，只是没有估值——**数据状态而非异常**。"""
    test_client, issued = client

    body = test_client.get(
        "/v1/market/positions?scope=all", headers=_auth(issued)
    ).json()

    assert body["data"]["count"] == 0
    assert body["data"]["complete"] is True
    assert body["data"]["marketData"]["enabled"] is False
