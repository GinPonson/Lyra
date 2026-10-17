"""行情路由测试。

三条要点：

1. **`routes` 与 `effectiveRoutes` 同时返回**——只返回前者会让用户看到"已选中
   alpaca"而每次请求都静默回退到备来源，这是最容易让用户困惑的一类配置问题。
2. **设置按用户读写**（V10）：A 保存来源不影响 B 的 revision，也不改写共享运行时；
   并发采用不同来源时各自拿到自己的来源。
3. **缓存与预算仍部署级共享**：公开报价缓存按报价身份复用，供应商预算全局生效；
   缓存里只有报价，不含任何用户的持仓。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from lyra.protocol import CLIENT_PROTOCOL_HEADERS
from lyra.application.device_pairing_service import DevicePairingService
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.infrastructure.db.user_repository import UserRepository
from lyra.main import create_app

# 夹具密钥只用于本地签名校验，不是任何真实凭据。
_KEY_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"]
PUBLIC_KEY_PEM = _KEY_CASES[0]["inputPem"]
SECOND_PUBLIC_KEY_PEM = _KEY_CASES[1]["inputPem"]
# 夹具内按身份顺序取密钥：每个用户必须用不同公钥，否则设备指纹会撞在一起。
_PEMS = (PUBLIC_KEY_PEM, SECOND_PUBLIC_KEY_PEM)
IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")
SECOND_IDENTITY = LoginIdentity(
    provider="google", subject="67890", email=None, display_name="other"
)
ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"
SECOND_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
_DEFAULT_UNDERLYING = object()


class _FakeResponse:
    """本地固定响应。"""

    status = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def chunks(self):
        """逐块返回预置正文。"""
        yield self._body


def _insert_account(
    connection,
    user_id: str,
    account_id: str,
    legacy_key: str,
    masked_suffix: str,
    *,
    base_currency: str = "USD",
) -> None:
    """插入一个归属该用户的账户。

    **`user_id` 是必填列**：账户现在按用户隔离，缺它会撞非空约束。
    """
    connection.execute(
        """
        INSERT INTO accounts (
            id, user_id, display_name, legacy_account_key, masked_suffix, base_currency
        ) VALUES (
            CAST(? AS UUID), CAST(? AS UUID), 'Account', ?, ?, ?
        )
        """,
        [account_id, user_id, legacy_key, masked_suffix, base_currency],
    )


def _issue_devices(application, connection, identities: list[LoginIdentity]):
    """为若干身份各签发一台设备。

    **所有签发必须在同一个事件循环里完成**：写协调器持有一把 ``asyncio.Lock``，
    它会在第一次 ``await`` 时绑定事件循环；分多次 ``asyncio.run`` 会让第二次因
    "bound to a different event loop" 直接失败。
    """

    async def pair_all():
        service = DevicePairingService(
            DuckDbDeviceRepository(connection),
            application.state.write_coordinator,
            UserRepository(connection),
        )
        return [
            await service.authorize_user_device(
                identity=identity,
                display_name=f"Device {index}",
                public_key_pem=_PEMS[index],
            )
            for index, identity in enumerate(identities)
        ]

    return asyncio.run(pair_all())


@contextmanager
def _market_app(tmp_path: Path, identities: list[LoginIdentity]):
    """装配一个多用户行情测试应用。

    @param tmp_path: 本用例独占的临时目录。
    @param identities: 需要预留用户与设备的身份列表；第一个身份额外获得主账户。
    @returns 上下文管理器，产出 ``(test_client, *issued)``。
    """
    database_path = tmp_path / "market.duckdb"
    database = open_lyra_database(str(database_path))
    connection = database.connection
    users = UserRepository(connection)
    now = datetime(2026, 10, 15, tzinfo=UTC)
    # 用户先于账户建立：账户现在必须带 user_id，且这里复用与登录路径同一套自动注册。
    resolved = [users.find_or_create_user_for_identity(i, now=now) for i in identities]
    _insert_account(connection, resolved[0].user.id, ACCOUNT_ID, "TST001", "001")
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    application = create_app(settings, database)
    issued = _issue_devices(application, connection, identities)
    try:
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
            yield (test_client, *issued)
    finally:
        database.close()


@pytest.fixture()
def client(tmp_path: Path):
    """单用户夹具：一个用户、一个账户、一台已配对设备。"""
    with _market_app(tmp_path, [IDENTITY]) as values:
        yield values


@pytest.fixture()
def two_users(tmp_path: Path):
    """双用户夹具：A（有主账户）与 B（无账户），各一台已配对设备。"""
    with _market_app(tmp_path, [IDENTITY, SECOND_IDENTITY]) as values:
        yield values


def _auth(issued) -> dict[str, str]:
    """构造已认证请求头。"""
    return {
        "Authorization": f"Bearer {issued.access_token}",
        # **每个 /v1 请求都必须声明客户端协议版本**（除公开路由）。
        # 缺失时服务端返回 426——那是客户端唯一能据此升级 App 的信号。
        # **引用共享常量而不是字面量**：写死会在协议升版后静默变成 426。
        **CLIENT_PROTOCOL_HEADERS,
    }


def _seed_stock_position(
    connection,
    user_id: str,
    account_id: str,
    symbol: str,
    *,
    quantity: str = "1",
    price: str = "10",
    underlying: str | None | object = _DEFAULT_UNDERLYING,
) -> None:
    """插入一笔用于行情路由测试的未平正股批次。

    账本版本行现在必须带 ``user_id``：用户当前账本版本是
    ``max(version) WHERE user_id = ?``（P0 契约 §3.3）。
    """
    transaction_hash = hashlib.sha256(
        f"{account_id}:{symbol}:{quantity}:{price}".encode("utf-8")
    ).hexdigest()
    resolved_underlying = symbol if underlying is _DEFAULT_UNDERLYING else underlying
    connection.execute(
        """
        INSERT INTO ledger_versions (user_id, reason, source_reference)
        VALUES (CAST(? AS UUID), 'file_import', ?)
        """,
        [user_id, transaction_hash],
    )
    version = connection.execute(
        "SELECT max(version) FROM ledger_versions WHERE user_id = CAST(? AS UUID)",
        [user_id],
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


def _marketdata_runtime(
    *,
    on_fetch: Callable[[str], None] | None = None,
    daily_request_limit: int | None = None,
):
    """创建使用本地固定响应的 MarketData.app 运行时。

    @param on_fetch: 每次上游请求回调符号，用于断言请求次数。
    @param daily_request_limit: 覆盖每日预算；``None`` 用配置默认。
    """
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
        return _FakeResponse(payload)

    environment = {
        "LYRA_MARKET_STOCK_PROVIDERS": "marketdata",
        "LYRA_MARKETDATA_MODE": "delayed",
        "LYRA_MARKETDATA_API_TOKEN": "test-only-token",
    }
    if daily_request_limit is not None:
        environment["LYRA_MARKETDATA_DAILY_REQUEST_LIMIT"] = str(daily_request_limit)
    return create_market_data_runtime(
        load_market_data_config(environment), fetch=fetch, now=lambda: now
    )


def _two_provider_runtime(*, on_fetch: Callable[[str], None] | None = None):
    """同时装配 marketdata 与 alpaca 的运行时，两个来源给出**明显不同**的价格。

    部署默认路由留空：本用例里两个用户各自保存自己的来源，不被部署默认值干扰。
    """
    from lyra.infrastructure.market_data.config import load_market_data_config
    from lyra.infrastructure.market_data.runtime import create_market_data_runtime

    now = int(datetime(2026, 10, 15, 20, tzinfo=UTC).timestamp() * 1000)
    fresh = int(datetime(2026, 10, 15, 15, tzinfo=UTC).timestamp())
    # Alpaca 的报价时点必须落在请求窗口 [cutoff-7d, cutoff] 内（cutoff = now-16min）。
    alpaca_timestamp = "2026-10-15T19:30:00Z"

    def fetch(url, *, headers, timeout):
        segments = urlsplit(url).path.rstrip("/").split("/")
        # Alpaca 的正股端点是 ``/v2/stocks/{symbol}/quotes``，末段是固定的 "quotes"。
        symbol = segments[-2] if segments[-1] == "quotes" else segments[-1]
        if on_fetch is not None:
            on_fetch(symbol)
        if "data.alpaca.markets" in url:
            payload = {
                "symbol": symbol,
                "quotes": [{"t": alpaca_timestamp, "bp": 99.9, "ap": 100.1}],
            }
        else:
            payload = {
                "s": "ok", "symbol": [symbol], "updated": [fresh],
                "bid": [10.0], "ask": [10.2], "last": [10.1],
            }
        return _FakeResponse(payload)

    return create_market_data_runtime(
        load_market_data_config({
            "LYRA_MARKET_STOCK_PROVIDERS": "",
            "LYRA_MARKETDATA_MODE": "delayed",
            "LYRA_MARKETDATA_API_TOKEN": "test-only-token",
            "LYRA_ALPACA_API_KEY": "test-only-key",
            "LYRA_ALPACA_SECRET_KEY": "test-only-secret",
        }),
        fetch=fetch,
        now=lambda: now,
    )


def _rows(test_client, issued) -> list[dict]:
    """读取当前用户的全部持仓行。"""
    body = test_client.get(
        "/v1/market/positions?scope=all", headers=_auth(issued)
    ).json()
    return body["data"]["rows"]


def _row_for(test_client, issued, symbol: str) -> dict:
    """读取当前用户某个符号的持仓行。"""
    rows = _rows(test_client, issued)
    matches = [row for row in rows if row["symbolKey"] == symbol]
    assert len(matches) == 1, rows
    return matches[0]


def _save_routes(test_client, issued, routes: dict, *, expected_revision: int = 0):
    """保存当前用户的来源设置并返回响应。"""
    return test_client.put(
        "/v1/market/settings",
        json={"expectedRevision": expected_revision, "routes": routes},
        headers=_auth(issued),
    )


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
    response = _save_routes(
        test_client, issued,
        {"stock": ["alpaca"], "option": ["marketdata"]},
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
    _save_routes(test_client, issued, {"stock": ["alpaca"]})

    response = _save_routes(test_client, issued, {"stock": ["marketdata"]})

    assert response.status_code == 409
    assert response.json()["code"] == "MARKET_SETTINGS_CHANGED"


def test_effective_routes_exclude_unconfigured_providers(client) -> None:
    """**`effectiveRoutes` 剔除未配置凭据的来源。**

    只返回 `routes` 会让用户看到"已选中 alpaca"而每次请求都静默回退到备来源。
    """
    test_client, issued = client
    # 本夹具没有配置任何行情凭据。
    body = _save_routes(
        test_client, issued, {"stock": ["alpaca", "marketdata"]}
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
    response = _save_routes(test_client, issued, {"crypto": ["alpaca"]})
    assert response.status_code == 400


def test_duplicate_provider_is_rejected(client) -> None:
    """重复来源必须拒绝。"""
    test_client, issued = client
    response = _save_routes(test_client, issued, {"stock": ["alpaca", "alpaca"]})
    assert response.status_code == 400


# --------------------------------------------------------------------------
# 按用户隔离（V10）
# --------------------------------------------------------------------------


def test_settings_and_revisions_are_isolated_per_user(two_users) -> None:
    """**V10：A 的设置更新不改变 B 的 revision**，两人各自读回自己的来源。

    单用户设计下 revision 是全局单例行的，A 保存会把 B 的 CAS 期望值一并推进——
    B 随后按 revision 0 保存会被误判为冲突。这里逐条钉住它不再发生。
    """
    test_client, issued_a, issued_b = two_users

    saved_a = _save_routes(test_client, issued_a, {"stock": ["marketdata"]})
    assert saved_a.status_code == 200
    assert saved_a.json()["revision"] == 1

    # B 从未保存过：仍是 revision 0、空路由、无保存时间。
    read_b = test_client.get("/v1/market/settings", headers=_auth(issued_b)).json()
    assert read_b["revision"] == 0
    assert read_b["routes"] == {"stock": [], "option": []}
    assert read_b["updatedAt"] is None

    # B 按自己的 revision 0 保存，得到自己的 revision 1。
    saved_b = _save_routes(test_client, issued_b, {"stock": ["alpaca"]})
    assert saved_b.status_code == 200
    assert saved_b.json()["revision"] == 1

    # A 的 revision 与来源都不受 B 的保存影响。
    read_a = test_client.get("/v1/market/settings", headers=_auth(issued_a)).json()
    assert read_a["revision"] == 1
    assert read_a["routes"]["stock"] == ["marketdata"]
    # B 读到的始终是自己的来源。
    read_b_again = test_client.get("/v1/market/settings", headers=_auth(issued_b)).json()
    assert read_b_again["routes"]["stock"] == ["alpaca"]


def test_concurrent_saves_use_each_users_own_revision(two_users) -> None:
    """**V10：两个用户并发保存时各自成功**，都从 revision 0 得到自己的 1。

    全局修订号下这两个请求必然有一个被 CAS 拒绝（后到者读到别人的 1）。
    """
    from concurrent.futures import ThreadPoolExecutor

    test_client, issued_a, issued_b = two_users

    def save(issued, provider: str):
        return _save_routes(test_client, issued, {"stock": [provider]})

    with ThreadPoolExecutor(max_workers=2) as executor:
        response_a = executor.submit(save, issued_a, "marketdata")
        response_b = executor.submit(save, issued_b, "alpaca")
        results = [response_a.result(timeout=30), response_b.result(timeout=30)]

    assert [response.status_code for response in results] == [200, 200]
    assert [response.json()["revision"] for response in results] == [1, 1]
    assert results[0].json()["routes"]["stock"] == ["marketdata"]
    assert results[1].json()["routes"]["stock"] == ["alpaca"]


def test_two_users_get_their_own_market_source(two_users) -> None:
    """**V10：A/B 采用不同来源时 HTTP 各自得到正确来源**，且不改写共享服务状态。"""
    test_client, issued_a, issued_b = two_users
    application = test_client.app
    connection = application.state.database.connection
    user_a, user_b = issued_a.user.id, issued_b.user.id
    _insert_account(connection, user_b, SECOND_ACCOUNT_ID, "TST002", "002")
    _seed_stock_position(connection, user_a, ACCOUNT_ID, "INTC")
    _seed_stock_position(connection, user_b, SECOND_ACCOUNT_ID, "INTC")

    runtime = _two_provider_runtime()
    application.state.market_runtime = runtime
    deployment_default = dict(runtime.service._routes)

    assert _save_routes(test_client, issued_a, {"stock": ["marketdata"]}).status_code == 200
    assert _save_routes(test_client, issued_b, {"stock": ["alpaca"]}).status_code == 200

    # **保存不得改写共享服务上的路由**：旧设计下这里会变成最后保存那个用户的路由，
    # 于是第一个用户的下一次请求会静默换成别人的来源。
    assert runtime.service._routes == deployment_default

    # 交错发两次请求：任何一个用户的请求都不会改变另一个用户的来源。
    for _ in range(2):
        row_a = _row_for(test_client, issued_a, "INTC")
        row_b = _row_for(test_client, issued_b, "INTC")

        assert row_a["quote"]["quote"]["providerId"] == "marketdata"
        assert Decimal(row_a["valuation"]["marketValue"]) == Decimal("10.1")
        assert row_b["quote"]["quote"]["providerId"] == "alpaca"
        assert Decimal(row_b["valuation"]["marketValue"]) == Decimal("100")

    market_a = test_client.get(
        "/v1/market/positions?scope=all", headers=_auth(issued_a)
    ).json()["data"]["marketData"]
    market_b = test_client.get(
        "/v1/market/positions?scope=all", headers=_auth(issued_b)
    ).json()["data"]["marketData"]
    assert market_a["routes"] == {"stock": ["marketdata"], "option": []}
    assert market_b["routes"] == {"stock": ["alpaca"], "option": []}


def test_provider_budget_is_shared_across_users(two_users) -> None:
    """**V10：供应商全局预算仍生效**——A 的调用吃掉 B 的额度。

    若把报价服务按用户复制，每个用户都会各自拿到全额额度，实际打给上游的请求量被
    放大到用户数倍（计划 §4.4）。
    """
    test_client, issued_a, issued_b = two_users
    application = test_client.app
    connection = application.state.database.connection
    user_a, user_b = issued_a.user.id, issued_b.user.id
    _insert_account(connection, user_b, SECOND_ACCOUNT_ID, "TST002", "002")
    _seed_stock_position(connection, user_a, ACCOUNT_ID, "INTC")
    # 两个持仓都用"新鲜报价"符号：本用例只关心预算是否被 A 用尽。
    _seed_stock_position(connection, user_a, ACCOUNT_ID, "AAPL")
    _seed_stock_position(connection, user_b, SECOND_ACCOUNT_ID, "AMD")

    fetched: list[str] = []
    application.state.market_runtime = _marketdata_runtime(
        on_fetch=fetched.append, daily_request_limit=2
    )
    assert _save_routes(test_client, issued_a, {"stock": ["marketdata"]}).status_code == 200
    assert _save_routes(test_client, issued_b, {"stock": ["marketdata"]}).status_code == 200

    rows_a = _rows(test_client, issued_a)
    # A 用掉了全部 2 次每日额度。
    assert sorted(fetched) == ["AAPL", "INTC"]
    assert {row["quote"]["status"] for row in rows_a} == {"available"}

    rows_b = _rows(test_client, issued_b)
    # B 的额度已被 A 用尽：不再打上游，明确报 rate_limited，而不是静默返回空估值。
    assert fetched == ["AAPL", "INTC"]
    assert rows_b[0]["symbolKey"] == "AMD"
    assert rows_b[0]["quote"]["status"] == "unavailable"
    assert rows_b[0]["quote"]["issue"] == "rate_limited"
    assert rows_b[0]["valuation"]["marketValue"] is None


def test_shared_quote_cache_is_reused_without_leaking_positions(two_users) -> None:
    """**V10：公共报价缓存可复用，且缓存值里没有用户持仓。**

    缓存键是公开报价身份（来源 + 合约），不是"谁在问"；用户持仓只出现在各自的响应
    行里，绝不进入共享缓存。
    """
    from lyra.domain.market_data.types import MarketQuote

    test_client, issued_a, issued_b = two_users
    application = test_client.app
    connection = application.state.database.connection
    user_a, user_b = issued_a.user.id, issued_b.user.id
    _insert_account(connection, user_b, SECOND_ACCOUNT_ID, "TST002", "002")
    _seed_stock_position(connection, user_a, ACCOUNT_ID, "INTC", quantity="3")
    _seed_stock_position(connection, user_b, SECOND_ACCOUNT_ID, "INTC", quantity="7")

    fetched: list[str] = []
    runtime = _marketdata_runtime(on_fetch=fetched.append)
    application.state.market_runtime = runtime
    assert _save_routes(test_client, issued_a, {"stock": ["marketdata"]}).status_code == 200
    assert _save_routes(test_client, issued_b, {"stock": ["marketdata"]}).status_code == 200

    row_a = _row_for(test_client, issued_a, "INTC")
    assert fetched == ["INTC"]
    assert row_a["accountId"] == ACCOUNT_ID
    assert Decimal(row_a["quantity"]) == Decimal("3")
    assert row_a["quote"]["cached"] is False
    # 同一份报价按各自持仓估值：A 的是 3 × 10.1。
    assert Decimal(row_a["valuation"]["marketValue"]) == Decimal("30.3")

    row_b = _row_for(test_client, issued_b, "INTC")
    # **第二次读取没有产生上游请求**：两个用户复用了同一条公开报价缓存。
    assert fetched == ["INTC"]
    assert row_b["accountId"] == SECOND_ACCOUNT_ID
    assert Decimal(row_b["quantity"]) == Decimal("7")  # B 看到的是自己的持仓
    assert row_b["quote"]["cached"] is True  # 报价来自共享缓存
    # 估值用的是 B 自己的持仓数量，不是缓存里（也不可能存在）别人的持仓。
    assert Decimal(row_b["valuation"]["marketValue"]) == Decimal("70.7")

    # 缓存只有一条，键是公开报价身份；值是一条报价，不含任何用户或账户标识。
    cache = runtime.service._cache
    assert list(cache) == ["marketdata\x00US:stock:INTC"]
    cached = next(iter(cache.values()))
    assert isinstance(cached.quote, MarketQuote)
    assert cached.quote.instrument_key == "US:stock:INTC"
    for identifier in (ACCOUNT_ID, SECOND_ACCOUNT_ID, user_a, user_b):
        assert all(identifier not in key for key in cache)


def test_positions_cannot_resolve_another_users_account(two_users) -> None:
    """**A 用 B 的账户 UUID 取行情持仓返回 404**，与"不存在"不可区分。

    账户 UUID 不带用户范围时，A 会解析出 B 的 legacy key，随后的账本查询就会返回
    B 的持仓（P0 契约 §3.11）。
    """
    test_client, issued_a, issued_b = two_users
    connection = test_client.app.state.database.connection
    _insert_account(connection, issued_b.user.id, SECOND_ACCOUNT_ID, "TST002", "002")
    _seed_stock_position(connection, issued_b.user.id, SECOND_ACCOUNT_ID, "AAPL")

    response = test_client.get(
        f"/v1/market/positions?scope={SECOND_ACCOUNT_ID}", headers=_auth(issued_a)
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ACCOUNT_NOT_FOUND"
    # 归属正确时仍可正常读取，说明拒绝的是归属而不是功能。
    own = test_client.get(
        f"/v1/market/positions?scope={SECOND_ACCOUNT_ID}", headers=_auth(issued_b)
    )
    assert own.status_code == 200
    assert [row["symbolKey"] for row in own.json()["data"]["rows"]] == ["AAPL"]


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
    _seed_stock_position(connection, issued.user.id, ACCOUNT_ID, "INTC", quantity="10", price="40")

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
    _insert_account(connection, issued.user.id, SECOND_ACCOUNT_ID, "TST002", "002")
    _seed_stock_position(connection, issued.user.id, ACCOUNT_ID, "INTC")
    _seed_stock_position(connection, issued.user.id, SECOND_ACCOUNT_ID, "AAPL")

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
    user_id = issued.user.id
    _insert_account(connection, user_id, SECOND_ACCOUNT_ID, "EUR002", "002", base_currency="EUR")
    _seed_stock_position(connection, user_id, ACCOUNT_ID, "INTC")
    _seed_stock_position(connection, user_id, ACCOUNT_ID, "MSFT")
    _seed_stock_position(connection, user_id, ACCOUNT_ID, "NVDA")
    # 空的券商标的身份无法构造行情合约；账户币种标注其未估值金额。
    _seed_stock_position(connection, user_id, SECOND_ACCOUNT_ID, "UNKNOWN", underlying="")
    test_client.app.state.market_runtime = _marketdata_runtime()

    saved = _save_routes(test_client, issued, {"stock": ["marketdata"]})
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


def test_saved_settings_are_read_per_user_after_restart(tmp_path: Path) -> None:
    """保存过的设置按**用户**读取；运行时不装载任何"全局当前设置"。

    这条取代了旧的"启动把库里的设置装进运行时"：那条路径把某一个用户的设置变成
    所有人的偏好（计划 §4.4 点名禁止）。
    """
    from lyra.domain.market_data.settings import normalize_routes
    from lyra.infrastructure.db.market_settings_repository import persist_market_settings

    database_path = tmp_path / "restarted-market.duckdb"
    database = open_lyra_database(str(database_path))
    connection = database.connection
    users = UserRepository(connection)
    now = datetime(2026, 10, 15, tzinfo=UTC)
    owner = users.find_or_create_user_for_identity(IDENTITY, now=now).user
    other = users.find_or_create_user_for_identity(SECOND_IDENTITY, now=now).user
    _insert_account(connection, owner.id, ACCOUNT_ID, "TST001", "001")
    persist_market_settings(
        connection, owner.id,
        expected_revision=0,
        routes=normalize_routes({"stock": ["marketdata"]}),
        now=now,
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

        snapshot = runtime.settings_snapshot(connection, owner.id)
        assert snapshot.revision == 1
        assert snapshot.routes == {"stock": ("marketdata",), "option": ()}
        assert snapshot.updated_at is not None

        # **另一个用户没有被装上别人的设置**：这正是"不再把某用户设置装成全局偏好"
        # 的可验证后果。
        other_snapshot = runtime.settings_snapshot(connection, other.id)
        assert other_snapshot.revision == 0
        assert other_snapshot.updated_at is None
        assert other_snapshot.routes != snapshot.routes

        issued = _issue_devices(application, connection, [IDENTITY])[0]
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
    user_id = issued.user.id
    _seed_stock_position(connection, user_id, ACCOUNT_ID, "INTC")
    snapshot_version = connection.execute(
        "SELECT max(version) FROM ledger_versions WHERE user_id = CAST(? AS UUID)",
        [user_id],
    ).fetchone()[0]
    inserted_versions: list[int] = []

    def write_during_quote(symbol: str) -> None:
        connection.execute(
            """
            INSERT INTO ledger_versions (user_id, reason, source_reference)
            VALUES (CAST(? AS UUID), 'file_import', ?)
            """,
            [user_id, f"quote-callback:{symbol}"],
        )
        inserted_versions.append(
            connection.execute(
                "SELECT max(version) FROM ledger_versions WHERE user_id = CAST(? AS UUID)",
                [user_id],
            ).fetchone()[0]
        )

    test_client.app.state.market_runtime = _marketdata_runtime(on_fetch=write_during_quote)
    saved = _save_routes(test_client, issued, {"stock": ["marketdata"]})
    assert saved.status_code == 200

    response = test_client.get(
        "/v1/market/positions?scope=all", headers=_auth(issued)
    )

    assert response.status_code == 200
    assert inserted_versions == [snapshot_version + 1]
    assert response.json()["metadata"]["ledgerVersion"] == snapshot_version
    assert connection.execute(
        "SELECT max(version) FROM ledger_versions WHERE user_id = CAST(? AS UUID)",
        [user_id],
    ).fetchone()[0] == snapshot_version + 1


# --------------------------------------------------------------------------
# 真实接线（反向验证）
# --------------------------------------------------------------------------


def test_network_fetch_runs_outside_any_database_transaction(client) -> None:
    """行情抓取期间**没有打开的事务**：网络延迟不得占住数据库锁（V17）。

    账本、修订号与路由要来自同一读快照，所以那段必须在一个事务里；但**抓取本身不能
    在事务内**——DuckDB 是单写库，一个慢的上游请求会把所有写操作一起卡住。

    判据取自 DuckDB 自身的行为而不是实现细节：已有事务时再开事务会抛
    ``TransactionException: cannot start a transaction within a transaction``。
    因此抓取回调里能成功开启事务，就证明调用点确实在 COMMIT 之后。
    """
    import duckdb

    test_client, issued = client
    connection = test_client.app.state.database.connection
    user_id = issued.user.id
    _seed_stock_position(connection, user_id, ACCOUNT_ID, "INTC")

    saw_free_connection: list[bool] = []

    def probe_during_quote(symbol: str) -> None:
        """抓取期间尝试开一个事务；成功说明当时没有事务在占用连接。"""
        try:
            connection.execute("BEGIN TRANSACTION")
        except duckdb.TransactionException:
            saw_free_connection.append(False)
        else:
            saw_free_connection.append(True)
            connection.execute("ROLLBACK")

    test_client.app.state.market_runtime = _marketdata_runtime(
        on_fetch=probe_during_quote
    )
    assert _save_routes(test_client, issued, {"stock": ["marketdata"]}).status_code == 200

    response = test_client.get(
        "/v1/market/positions?scope=all", headers=_auth(issued)
    )

    assert response.status_code == 200
    # 至少发生过一次上游抓取，否则这条断言会变成"什么都没测"的空转。
    assert saw_free_connection, "本次请求没有触发任何上游抓取，断言失去意义"
    assert saw_free_connection == [True], (
        "抓取期间连接上仍有打开的事务——慢的上游请求会占住数据库锁"
    )


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
