"""账本只读端点的认证与形状测试。

覆盖三件事：

1. **未认证一律 401**，且不区分令牌不存在、设备不存在与已过期——否则未认证
   调用方可以靠错误码枚举出哪些令牌曾经存在；
2. **认证通过后返回领域结果**；
3. **金额以十进制字符串出站**——FastAPI 的默认编码器会把 ``Decimal`` 转成
   ``float``，那会丢精度，违反项目既定的金额传输规则。
"""

from __future__ import annotations

import asyncio
import hashlib
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

KEYS = Path(__file__).parent / "fixtures" / "device-keys.json"
PUBLIC_KEY_PEM = __import__("json").loads(KEYS.read_text(encoding="utf-8"))["cases"][0]["inputPem"]
IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")
ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"


def _h(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _seed_ledger(connection) -> None:
    """写入一条买入 + 一条卖出，使总览与回撤都有内容。"""
    connection.execute(
        "INSERT INTO ledger_versions (version, reason, source_reference) VALUES (1, 'file_import', 'src')"
    )
    connection.execute(
        """
        INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), 'Account', 'TST001', '001')
        """,
        [ACCOUNT_ID],
    )
    for index, (action, date, price, amount) in enumerate(
        [
            ("Buy", "2026-01-05", "40.00", "-400.00"),
            ("Sell", "2026-01-10", "60.00", "600.00"),
        ],
        start=1,
    ):
        connection.execute(
            """
            INSERT INTO transactions
                (account_id, record_date, transaction_date, action, raw_symbol, description,
                 asset_type, underlying, quantity, price, amount, source_type, source_reference,
                 source_hash, sequence, occurrence, business_hash, transaction_hash,
                 parser_version, ledger_version)
            VALUES (CAST(? AS UUID), CAST(? AS DATE), CAST(? AS DATE), ?, 'INTC', 'INTEL CORP',
                    'stock', 'INTC', CAST('10' AS DECIMAL(18,6)), CAST(? AS DECIMAL(18,6)),
                    CAST(? AS DECIMAL(18,4)), 'csv', 'src', ?, ?, 1, ?, ?, 2, 1)
            """,
            [ACCOUNT_ID, date, date, action, price, amount, _h("source"), index,
             _h(f"business-{index}"), _h(f"txn-{index}")],
        )


@pytest.fixture()
def client(tmp_path: Path):
    """构造已认证可用的应用与一个有效访问令牌。"""
    database_path = tmp_path / "api.duckdb"
    database = open_lyra_database(str(database_path))
    _seed_ledger(database.connection)

    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    application = create_app(settings, database)
    repository = DuckDbDeviceRepository(database.connection)
    service = DevicePairingService(repository, WriteCoordinator())
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


def _summary_data(body: dict) -> dict:
    """取出总览信封里的第一个币种分组数据。"""
    assert body["aggregateCurrency"] == "USD"
    assert len(body["groups"]) == 1
    return body["groups"][0]["data"]


def test_rejects_missing_token(client) -> None:
    """缺少授权头必须 401。"""
    test_client, _ = client
    assert test_client.get("/v1/ledger/summary").status_code == 401


@pytest.mark.parametrize("header", ["Bearer ", "Bearer not-a-real-token", "Basic abc", "token"])
def test_rejects_invalid_authorization(client, header: str) -> None:
    """非法授权头必须 401，且不区分失败原因。"""
    test_client, _ = client
    response = test_client.get("/v1/ledger/summary", headers={"Authorization": header})
    assert response.status_code == 401


def test_rejects_revoked_token(client) -> None:
    """设备撤销后原访问令牌必须立即失效。"""
    test_client, issued = client
    repository = DuckDbDeviceRepository(test_client.app.state.database.connection)
    asyncio.run(DevicePairingService(repository, WriteCoordinator()).revoke_device(issued.device_id))

    response = test_client.get("/v1/ledger/summary", headers=_auth(issued), params={"scope": "all"})
    assert response.status_code == 401


def test_summary_returns_ledger_result(client) -> None:
    """认证通过后应返回账本总览。"""
    test_client, issued = client
    response = test_client.get("/v1/ledger/summary", headers=_auth(issued), params={"scope": "all"})

    assert response.status_code == 200
    data = _summary_data(response.json())
    # 公开字段是 camelCase，与客户端协议 2 一致。
    assert data["transactionCount"] == 2
    assert data["realizedPnlTotal"] == "200.0000"


def test_amounts_are_decimal_strings_not_floats(client) -> None:
    """金额必须是字符串：默认编码器会把 Decimal 转 float，那会丢精度。"""
    test_client, issued = client
    data = _summary_data(test_client.get("/v1/ledger/summary", headers=_auth(issued), params={"scope": "all"}).json())

    for key in ("netDeposits", "realizedPnlTotal", "cashBalance", "totalFees"):
        assert isinstance(data[key], str), f"{key} 必须是十进制字符串"
        assert not isinstance(data[key], float)


def test_positions_endpoint_shape(client) -> None:
    """持仓端点返回 rows/count 信封。"""
    test_client, issued = client
    body = test_client.get("/v1/ledger/positions", headers=_auth(issued), params={"scope": "all"}).json()

    assert body["count"] == 0
    assert body["rows"] == []


def test_realized_endpoint_returns_decimal_strings(client) -> None:
    """已实现明细的金额同样是字符串。"""
    test_client, issued = client
    body = test_client.get("/v1/ledger/realized", headers=_auth(issued), params={"scope": "all"}).json()

    assert body["count"] == 1
    assert isinstance(body["totalPnl"], str)
    row = body["rows"][0]
    assert isinstance(row["pnl"], str)
    # 公开字段是 quantity，且不再泄漏开平仓哈希。
    assert isinstance(row["quantity"], str)
    assert "qty" not in row
    assert "open_txn" not in row
    assert "closeTxn" not in row


def test_drawdown_endpoint_uses_currency_envelope(client) -> None:
    """回撤端点返回币种信封，且公开字段为 camelCase。"""
    test_client, issued = client
    body = test_client.get("/v1/analytics/drawdown", headers=_auth(issued), params={"scope": "all"}).json()

    assert body["aggregateCurrency"] == "USD"
    assert len(body["groups"]) == 1
    data = body["groups"][0]["data"]
    assert data["endingCumulativePnl"] == "200.0000"
    assert data["days"] == 1
    # 公开字段不再出现 snake_case 名字。
    assert "ending_cumulative_pnl" not in data
    assert data["rows"][0]["cumulativePnl"] == "200.0000"


def test_expiring_requires_as_of(client) -> None:
    """``as_of`` 必填：缺省会让结果随服务器日期漂移。"""
    test_client, issued = client
    assert test_client.get(
        "/v1/ledger/expiring", headers=_auth(issued), params={"scope": "all"}
    ).status_code == 400


def test_scope_parameter_is_required(client) -> None:
    """**``?scope=`` 是必填的**，缺失时 400 而不是默认"全部账户"。

    参考实现的错误信息说明了意图："请求必须明确指定单个账户或全部活跃账户"。
    **"查全部账户"必须是一个显式的选择**——否则一个忘记带参数的客户端会拿到
    **全部账户的数据**，而不是一个错误。对私有账本应用，这个默认方向是反的。
    """
    test_client, issued = client

    # 缺失 → 400，且错误码是稳定的。
    missing = test_client.get("/v1/ledger/summary", headers=_auth(issued))
    assert missing.status_code == 400
    assert missing.json()["code"] == "ACCOUNT_SCOPE_REQUIRED"

    # 显式 all → 正常
    assert test_client.get(
        "/v1/ledger/summary", headers=_auth(issued), params={"scope": "all"}
    ).status_code == 200

    # 非法取值 → 400（不是静默当成 all）
    assert test_client.get(
        "/v1/ledger/summary", headers=_auth(issued), params={"scope": "TST001"}
    ).status_code == 400


def test_an_unknown_account_uuid_is_a_404_not_an_empty_ledger(client) -> None:
    """**账户 UUID 不存在时报 404，而不是返回空账本。**

    返回空账本会让客户端以为"这个账户没有交易"，而实际上是它根本不存在。
    """
    test_client, issued = client
    response = test_client.get(
        "/v1/ledger/summary", headers=_auth(issued),
        params={"scope": "99999999-9999-9999-9999-999999999999"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ACCOUNT_NOT_FOUND"



def test_all_read_routes_require_auth(client) -> None:
    """**每条只读路由都必须要求认证**，不能有遗漏的公开端点。"""
    test_client, _ = client
    for path in (
        "/v1/ledger/accounts", "/v1/ledger/summary", "/v1/ledger/positions",
        "/v1/ledger/trades",
        "/v1/ledger/realized", "/v1/ledger/cashflow", "/v1/ledger/audit",
        "/v1/analytics/monthly", "/v1/analytics/allocation", "/v1/analytics/attribution",
        "/v1/analytics/drawdown", "/v1/analytics/holding-period",
        "/v1/analytics/extremes", "/v1/analytics/streaks", "/v1/analytics/stress",
    ):
        assert test_client.get(path).status_code == 401, f"{path} 未要求认证"


def test_position_projection_redacts_account_and_uses_camel_case(client) -> None:
    """持仓公开字段必须脱敏账户并改用 camelCase。

    账户引用从脱敏键换成内部 UUID；若投影漏了这一步，客户端会拿到服务端内部的
    账户键，属于信息泄漏。
    """
    test_client, issued = client
    connection = test_client.app.state.database.connection
    connection.execute(
        """
        INSERT INTO lots
            (account_id, symbol_key, asset_type, underlying, expiry, strike, option_type,
             direction, open_date, open_price,
             open_quantity, remaining_quantity, open_fees, fees_allocated,
             open_transaction_hash, ledger_version)
        VALUES (CAST(? AS UUID), 'INTC 03/19/2027 100.00 C', 'option', 'INTC',
                CAST('2027-03-19' AS DATE), CAST('100.00' AS DECIMAL(18,4)), 'C',
                'long', CAST('2026-01-05' AS DATE), CAST('2.50' AS DECIMAL(18,6)),
                CAST('2' AS DECIMAL(18,6)), CAST('2' AS DECIMAL(18,6)),
                CAST('0.66' AS DECIMAL(18,6)), CAST('0' AS DECIMAL(18,6)),
                ?, 1)
        """,
        [ACCOUNT_ID, _h("txn-1")],
    )

    body = test_client.get("/v1/ledger/positions", headers=_auth(issued), params={"scope": "all"}).json()

    assert body["count"] == 1
    assert body["basis"] == "historical_cost_not_market_value"
    row = body["rows"][0]
    # 账户脱敏为内部 UUID，不再是脱敏键。
    assert row["accountId"] == ACCOUNT_ID
    assert "account" not in row
    # camelCase 字段与十进制字符串。
    assert row["symbolKey"] == "INTC 03/19/2027 100.00 C"
    assert row["firstOpenDate"] == "2026-01-05"
    assert row["quantity"] == "2"
    assert isinstance(row["cost"], str)
    assert "qty" not in row


def test_expiring_projection_uses_camel_case(client) -> None:
    """到期端点的统计字段同样是 camelCase。"""
    test_client, issued = client
    body = test_client.get(
        "/v1/ledger/expiring", headers=_auth(issued),
        params={"as_of": "2026-09-01", "scope": "all"},
    ).json()

    assert set(body) == {"rows", "count", "asOf", "cutoff", "expired", "expiresToday", "upcoming"}
    assert body["asOf"] == "2026-09-01"


# 各 analytics 端点的公开 data 中必须出现的 camelCase 字段（每条命令至少一个）。
_ENVELOPE_CASES = [
    ("/v1/analytics/monthly", "totalPnl"),
    ("/v1/analytics/allocation", "totalCostBasis"),
    ("/v1/analytics/attribution", "totalPnl"),
    ("/v1/analytics/holding-period", "buckets"),
    ("/v1/analytics/streaks", "closeTransactions"),
    ("/v1/analytics/stress", "longOptionContracts"),
]


@pytest.mark.parametrize(("path", "public_field"), _ENVELOPE_CASES)
def test_analytics_endpoints_return_currency_envelope(client, path: str, public_field: str) -> None:
    """这些命令都返回币种信封，且 data 是 camelCase 公开字段。"""
    test_client, issued = client
    body = test_client.get(
        path, headers=_auth(issued), params={"scope": "all"}
    ).json()

    assert set(body) == {"aggregateCurrency", "groups"}, path
    assert body["aggregateCurrency"] == "USD", path
    assert len(body["groups"]) == 1, path
    group = body["groups"][0]
    assert group["currency"] == "USD"
    # 账户引用是内部 UUID，而不是脱敏键。
    assert group["accountIds"] == [ACCOUNT_ID]
    assert public_field in group["data"], f"{path} 缺少 {public_field}"


def test_risk_endpoint_requires_as_of_and_returns_envelope(client) -> None:
    """风险端点 ``as_of`` 必填，且返回信封。"""
    test_client, issued = client
    assert test_client.get(
        "/v1/analytics/risk", headers=_auth(issued), params={"scope": "all"}
    ).status_code == 400

    body = test_client.get(
        "/v1/analytics/risk", headers=_auth(issued),
        params={"as_of": "2026-09-01", "scope": "all"},
    ).json()
    assert body["aggregateCurrency"] == "USD"
    data = body["groups"][0]["data"]
    assert data["asOf"] == "2026-09-01"
    assert "optionPositions" in data
    assert "as_of" not in data


def test_envelope_data_has_no_snake_case_keys(client) -> None:
    """公开 data 里不得残留 snake_case 字段名。

    投影是显式逐字段列出的，漏一个字段就会发出客户端读不懂的名字。
    """
    test_client, issued = client
    for path, _ in _ENVELOPE_CASES:
        data = test_client.get(
            path, headers=_auth(issued), params={"scope": "all"}
        ).json()["groups"][0]["data"]
        for key in data:
            # **``request_id`` 是唯一允许的 snake_case 键**：它是日志关联标识而非领域
            # 字段，参考实现也用这个名字（见 `api-error.ts` 的 `createResponse`）。
            # 其余任何下划线键都是投影漏改的信号。
            if key == "request_id":
                continue
            assert "_" not in key, f"{path} 泄漏 snake_case 字段: {key}"


def test_trades_projection_hides_hashes_and_uses_camel_case(client) -> None:
    """交易投影必须隐藏哈希与来源引用，并把 symbol 取自 raw_symbol。"""
    test_client, issued = client
    body = test_client.get("/v1/ledger/trades", headers=_auth(issued), params={"scope": "all"}).json()

    assert body["count"] == 2
    # 查询按交易日期倒序，因此首行是卖出。
    row = next(item for item in body["rows"] if item["action"] == "Buy")
    assert row["accountId"] == ACCOUNT_ID
    assert row["symbol"] == "INTC"
    assert row["sourceType"] == "csv"
    # 这些字段刻意不展开给客户端。
    for hidden in ("txn_hash", "sourceHash", "sourceReference", "businessHash", "occurrence"):
        assert hidden not in row, f"公开交易泄漏了 {hidden}"
    assert row["quantity"] == "10"
    assert row["price"] == "40"
    assert body["rows"][0]["action"] == "Sell"


def test_cashflow_projection_includes_action_totals(client) -> None:
    """现金流水投影带分动作合计，且按哈希关联回完整交易。

    买入/卖出不是现金流，因此先插入一笔股息。
    """
    test_client, issued = client
    connection = test_client.app.state.database.connection
    connection.execute(
        """
        INSERT INTO transactions
            (account_id, record_date, transaction_date, action, description, asset_type,
             amount, source_type, source_reference, source_hash, sequence, occurrence,
             business_hash, transaction_hash, parser_version, ledger_version)
        VALUES (CAST(? AS UUID), CAST('2026-02-01' AS DATE), CAST('2026-02-01' AS DATE),
                'Qualified Dividend', 'INTEL DIVIDEND', 'cash', CAST('12.3400' AS DECIMAL(18,4)),
                'csv', 'src', ?, 3, 1, ?, ?, 2, 1)
        """,
        [ACCOUNT_ID, _h("source"), _h("business-3"), _h("txn-3")],
    )

    body = test_client.get("/v1/ledger/cashflow", headers=_auth(issued), params={"scope": "all"}).json()

    assert body["count"] == 1
    assert isinstance(body["total"], str)
    assert body["byAction"] == {"Qualified Dividend": "12.3400"}
    # 窄行关联回完整交易后才有 sourceType。
    assert body["rows"][0]["sourceType"] == "csv"
    assert body["rows"][0]["accountId"] == ACCOUNT_ID


def test_cashflow_is_empty_without_cash_transactions(client) -> None:
    """没有现金类交易时返回空结果，而不是把买卖也算进来。"""
    test_client, issued = client
    body = test_client.get("/v1/ledger/cashflow", headers=_auth(issued), params={"scope": "all"}).json()

    assert body["count"] == 0
    assert body["rows"] == []
    assert body["byAction"] == {}


def test_pagination_fields_are_truthful_not_fabricated(client) -> None:
    """分页字段**必须是真的**，不能伪造。

    这条测试的**原则没变、表述随实现演进**：

    - 伪造 ``totalCount = count`` 在截断时是错的（``count`` 只是本页条数）；
    - 伪造 ``nextCursor`` 会让客户端无限翻页。

    它曾以"字段不存在"的形式存在（功能未实现时的守则）。现在三条命令的分页字段都已
    实现，因此断言改为**值的真实性**：

    - 数据不足一页时 ``nextCursor`` 必须是 ``null``（而不是一个空游标）；
    - ``totalCount`` 若存在，必须等于**全量条数**（夹具下等于当前页条数）；
    - ``trades`` 尚未计算总数，因此该字段必须**不存在**——宁可缺字段，也不发假数据。
    """
    test_client, issued = client

    for path in ("/v1/ledger/trades", "/v1/ledger/realized", "/v1/ledger/cashflow"):
        body = test_client.get(
            path, headers=_auth(issued), params={"scope": "all"}
        ).json()

        # 夹具数据不足一页，因此必须是 null，而不是被截断出来的游标。
        assert body["nextCursor"] is None, f"{path} 伪造了 nextCursor"
        if body["nextCursor"] is not None:
            assert isinstance(body["nextCursor"], str) and body["nextCursor"] != ""

        if path.endswith("trades"):
            assert "totalCount" not in body, "trades 尚未计算总数，不得发一个假值"
        else:
            assert body["totalCount"] == body["count"], (
                f"{path} 的 totalCount 与全量不符"
            )

def test_audit_projection_is_camel_case(client) -> None:
    """审计报告为 camelCase，且不含旧账户键。"""
    test_client, issued = client
    body = test_client.get("/v1/ledger/audit", headers=_auth(issued), params={"scope": "all"}).json()

    assert body["readOnly"] is True
    assert body["transactionCount"] == 2
    assert isinstance(body["ok"], bool)
    assert "scopeNote" in body
    assert "scope_note" not in body
    assert body["latestTransactionDate"] == "2026-01-10"
    # 检查项本身也是 camelCase 字段名。
    for row in body["rows"]:
        assert set(row) == {"check", "severity", "count", "expectation", "status"}


def test_extremes_projection_hides_hashes_and_redacts_account(client) -> None:
    """极值交易只公开业务字段，账户脱敏为 UUID，且不含交易哈希。"""
    test_client, issued = client
    body = test_client.get("/v1/analytics/extremes", headers=_auth(issued), params={"scope": "all"}).json()

    assert body["perSide"] == 10
    assert body["basis"]
    # 只有一笔平仓交易时，它**同时**是最好与最差，因此两侧各出一行。
    assert len(body["rows"]) == 2
    assert {row["side"] for row in body["rows"]} == {"best", "worst"}
    assert all(row["rank"] == 1 for row in body["rows"])

    row = body["rows"][0]
    trade = row["trade"]
    assert trade["accountId"] == ACCOUNT_ID
    assert trade["quantity"] == "10"
    assert trade["closeDate"] == "2026-01-10"
    for hidden in ("account", "openTransaction", "closeTransaction", "open_txn", "close_txn"):
        assert hidden not in trade, f"极值交易泄漏了 {hidden}"


# 全部只读端点：公开响应里不得出现 snake_case 字段名。
_ALL_READ_PATHS = [
    "/v1/ledger/accounts", "/v1/ledger/summary", "/v1/ledger/positions",
    "/v1/ledger/expiring?as_of=2026-09-01",
    "/v1/ledger/trades", "/v1/ledger/realized", "/v1/ledger/cashflow", "/v1/ledger/audit",
    "/v1/analytics/monthly", "/v1/analytics/allocation", "/v1/analytics/attribution",
    "/v1/analytics/drawdown", "/v1/analytics/risk?as_of=2026-09-01",
    "/v1/analytics/holding-period", "/v1/analytics/extremes",
    "/v1/analytics/streaks", "/v1/analytics/stress",
]


def _snake_keys(value: object, prefix: str = "") -> list[str]:
    """递归收集响应里所有含下划线的键（``byAction`` 的字典键是 Action 名，豁免）。"""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if "_" in key:
                found.append(f"{prefix}{key}")
            found.extend(_snake_keys(child, f"{prefix}{key}."))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_snake_keys(child, f"{prefix}[{index}]."))
    return found


def test_no_read_endpoint_leaks_snake_case_keys(client) -> None:
    """所有只读端点的公开响应里都不得残留 snake_case 字段名。

    投影是显式逐字段列出的，漏一个字段就会把内部命名发给客户端——客户端只会读到
    ``undefined``，不会报错。这条断言覆盖全部端点，防止将来新增端点时漏投影。
    """
    test_client, issued = client
    for path in _ALL_READ_PATHS:
        body = test_client.get(
        path, headers=_auth(issued), params={"scope": "all"}
    ).json()
        leaked = [key for key in _snake_keys(body) if key != "request_id"]
        # **``request_id`` 是唯一允许的 snake_case 键**：它是日志关联标识而非领域
        # 字段，参考实现也用这个名字。其余下划线键都是投影漏改的信号。
        assert not leaked, f"{path} 泄漏 snake_case 字段: {leaked}"


def test_accounts_endpoint_exposes_public_fields(client) -> None:
    """账户端点返回 camelCase 字段，且不暴露券商完整标识。"""
    test_client, issued = client
    body = test_client.get("/v1/ledger/accounts", headers=_auth(issued), params={"scope": "all"}).json()

    assert body["count"] == 1
    row = body["rows"][0]
    assert row["id"] == ACCOUNT_ID
    assert row["maskedSuffix"] == "001"
    assert row["baseCurrency"] == "USD"
    assert row["status"] == "active"
    assert set(row) == {
        "id", "displayName", "broker", "maskedSuffix", "baseCurrency", "status", "createdAt",
    }
    assert "legacyAccountKey" not in row, "公开账户泄漏了账本账户键"


def test_an_invalid_cursor_is_a_400_not_a_silent_first_page(client) -> None:
    """**无效游标报 400，绝不回退到第一页。**

    回退会让客户端把"游标坏了"当成"数据到头了"——它会以为已经看完全部数据，
    而实际上只看了第一页。这比报错危险得多，因为它**不报错**。
    """
    test_client, issued = client
    response = test_client.get(
        "/v1/ledger/trades", headers=_auth(issued),
        params={"scope": "all", "cursor": "not-a-valid-cursor!"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_CURSOR"


def test_a_cursor_from_a_different_scope_is_rejected(client) -> None:
    """**别的查询条件生成的游标不可用。**

    游标绑定账户范围与筛选条件；跨查询使用会从"另一个结果集里的某一行"继续，
    **返回的行错位且不报错**。
    """
    import base64, json

    foreign = (
        base64.urlsafe_b64encode(json.dumps({
            "version": 1,
            "transactionHash": "deadbeef",
            "filterFingerprint": "0" * 64,
        }).encode()).rstrip(b"=").decode()
    )
    test_client, issued = client
    response = test_client.get(
        "/v1/ledger/trades", headers=_auth(issued),
        params={"scope": "all", "cursor": foreign},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_CURSOR"


def test_no_cursor_means_the_first_page(client) -> None:
    """不带游标即第一页。"""
    test_client, issued = client
    body = test_client.get(
        "/v1/ledger/trades", headers=_auth(issued), params={"scope": "all"}
    ).json()

    assert body["count"] >= 0
    # 夹具数据不足一页，因此没有下一页。
    assert body["nextCursor"] is None
