"""HTTP 回归测试：账本明细端点必须把公开筛选参数交给领域查询。"""

from __future__ import annotations

import pytest

import test_ledger_routes as route_test_helpers

# 复用既有路由测试 fixture，避免新建一套认证与临时数据库初始化逻辑。
client = route_test_helpers.client


@pytest.fixture
def ledger_client(request: pytest.FixtureRequest):
    """返回既有 ``client`` fixture 创建的认证测试客户端。"""
    return request.getfixturevalue("client")


def test_trades_route_filters_and_paginates_with_bound_cursor(ledger_client) -> None:
    """交易流水按 symbol/action/日期筛选，且续页游标绑定这些筛选条件。"""
    test_client, issued = ledger_client
    route_test_helpers._insert_realized_rows(test_client.app.state.database.connection, count=3)
    params = {
        "scope": "all",
        "symbol": "MSFT",
        "action": "Sell",
        "fromDate": "2026-02-02",
        "toDate": "2026-02-06",
        "limit": 1,
    }

    first = route_test_helpers._data(test_client.get(
        "/v1/ledger/trades", headers=route_test_helpers._auth(issued), params=params
    ).json())
    assert first["count"] == 1
    assert first["rows"][0]["symbol"] == "MSFT"
    assert first["rows"][0]["action"] == "Sell"
    assert first["rows"][0]["transactionDate"] == "2026-02-06"
    assert first["nextCursor"]

    second = route_test_helpers._data(test_client.get(
        "/v1/ledger/trades",
        headers=route_test_helpers._auth(issued),
        params={**params, "cursor": first["nextCursor"]},
    ).json())
    assert second["count"] == 1
    assert second["rows"][0]["transactionDate"] == "2026-02-04"
    assert second["nextCursor"]

    last = route_test_helpers._data(test_client.get(
        "/v1/ledger/trades",
        headers=route_test_helpers._auth(issued),
        params={**params, "cursor": second["nextCursor"]},
    ).json())
    assert last["count"] == 1
    assert last["rows"][0]["transactionDate"] == "2026-02-02"
    assert last["nextCursor"] is None

    changed_filter = test_client.get(
        "/v1/ledger/trades",
        headers=route_test_helpers._auth(issued),
        params={**params, "action": "Buy", "cursor": first["nextCursor"]},
    )
    assert changed_filter.status_code == 400
    assert changed_filter.json()["code"] == "INVALID_CURSOR"


def test_realized_route_filters_dates_and_paginates_with_bound_cursor(ledger_client) -> None:
    """已实现明细按标的与平仓日期筛选，翻页不改变全量合计。"""
    test_client, issued = ledger_client
    route_test_helpers._insert_realized_rows(test_client.app.state.database.connection, count=3)
    params = {
        "scope": "all",
        "symbol": "MSFT",
        "fromDate": "2026-02-02",
        "toDate": "2026-02-06",
        "limit": 2,
    }

    first = route_test_helpers._data(test_client.get(
        "/v1/ledger/realized", headers=route_test_helpers._auth(issued), params=params
    ).json())
    assert first["count"] == 2
    assert first["totalCount"] == 3
    assert first["totalPnl"] == "3.0000"
    assert [row["closeDate"] for row in first["rows"]] == [
        "2026-02-06", "2026-02-04",
    ]
    assert first["nextCursor"]

    second = route_test_helpers._data(test_client.get(
        "/v1/ledger/realized",
        headers=route_test_helpers._auth(issued),
        params={**params, "cursor": first["nextCursor"]},
    ).json())
    assert second["count"] == 1
    assert second["totalCount"] == 3
    assert second["totalPnl"] == "3.0000"
    assert second["rows"][0]["closeDate"] == "2026-02-02"
    assert second["nextCursor"] is None

    changed_filter = test_client.get(
        "/v1/ledger/realized",
        headers=route_test_helpers._auth(issued),
        params={**params, "fromDate": "2026-02-04", "cursor": first["nextCursor"]},
    )
    assert changed_filter.status_code == 400
    assert changed_filter.json()["code"] == "INVALID_CURSOR"


def test_cashflow_route_filters_dates_and_paginates_with_bound_cursor(ledger_client) -> None:
    """现金流水按交易日期筛选，且游标续页沿用过滤后的合计。"""
    test_client, issued = ledger_client
    route_test_helpers._insert_cashflow_rows(test_client.app.state.database.connection, count=4)
    params = {
        "scope": "all",
        "fromDate": "2026-06-02",
        "toDate": "2026-06-04",
        "limit": 2,
    }

    first = route_test_helpers._data(test_client.get(
        "/v1/ledger/cashflow", headers=route_test_helpers._auth(issued), params=params
    ).json())
    assert first["count"] == 2
    assert first["totalCount"] == 3
    assert first["total"] == "9.0000"
    assert [row["transactionDate"] for row in first["rows"]] == [
        "2026-06-04", "2026-06-03",
    ]
    assert first["nextCursor"]

    second = route_test_helpers._data(test_client.get(
        "/v1/ledger/cashflow",
        headers=route_test_helpers._auth(issued),
        params={**params, "cursor": first["nextCursor"]},
    ).json())
    assert second["count"] == 1
    assert second["totalCount"] == 3
    assert second["total"] == "9.0000"
    assert second["rows"][0]["transactionDate"] == "2026-06-02"
    assert second["nextCursor"] is None

    changed_filter = test_client.get(
        "/v1/ledger/cashflow",
        headers=route_test_helpers._auth(issued),
        params={**params, "toDate": "2026-06-03", "cursor": first["nextCursor"]},
    )
    assert changed_filter.status_code == 400
    assert changed_filter.json()["code"] == "INVALID_CURSOR"


@pytest.mark.parametrize(
    "path",
    ("/v1/ledger/trades", "/v1/ledger/realized", "/v1/ledger/cashflow"),
)
@pytest.mark.parametrize(
    "invalid_filter",
    (
        {"fromDate": "not-a-date"},
        {"limit": 201},
    ),
)
def test_query_routes_reject_invalid_dates_and_limits(
    ledger_client,
    path: str,
    invalid_filter: dict,
) -> None:
    """三种明细查询都通过领域校验拒绝非法日期与超限页长。"""
    test_client, issued = ledger_client

    response = test_client.get(
        path,
        headers=route_test_helpers._auth(issued),
        params={"scope": "all", **invalid_filter},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"
