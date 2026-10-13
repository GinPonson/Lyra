"""路由契约（签入的清单）。

**为什么是签入的清单而不是与另一个实现比对**：迁移前本文件对照的是 TypeScript 版的
路由注册源码。TypeScript 服务端已移除，那份耦合也随之消失——**但保护不能消失**。

契约改为记录在 `fixtures/route-manifest.json`：新增或删除路由时必须同时改它，否则这条
测试会失败。保护的形式从"比对"变成了"回归检测"。

**清单必须是 (方法, 路径) 对，不能只是路径集合**——只比路径会漏掉"某条路径上少了一个
方法"。这正是迁移期间真实发生过的误判：`POST /v1/ledger/accounts` 整块缺失，而路径级
对账把它算作一致（因为 `GET /v1/ledger/accounts` 存在）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

import pytest

MANIFEST = Path(__file__).parent / "fixtures" / "route-manifest.json"


def _normalize(path: str) -> str:
    """把路径参数归一化为 ``{}``。

    **参数名不是契约的一部分**（两侧各有命名习惯），路径形状才是。
    """
    return re.sub(r"\{[^}]+\}", "{}", path)


@pytest.fixture(scope="module")
def served_pairs() -> set[tuple[str, str]]:
    """实际装配出来的 (方法, 路径) 对。"""
    from lyra.config import Settings
    from lyra.db.connection import open_lyra_database
    from lyra.main import create_app

    with tempfile.TemporaryDirectory() as work:
        database = open_lyra_database(str(Path(work) / "routes.duckdb"))
        try:
            settings = Settings(
                LYRA_DATABASE_PATH=str(Path(work) / "routes.duckdb"),
                LYRA_IMPORT_STAGING_DIR=str(Path(work) / "staging"),
                LYRA_SECRET_MASTER_KEY="0" * 64,
            )
            app = create_app(settings, database)
            pairs = {
                (method.upper(), _normalize(path))
                for path, ops in app.openapi()["paths"].items()
                if path.startswith("/v1")
                for method in ops
            }
            # **WebSocket 不在这里确认。**
            #
            # 它既不在 OpenAPI 里，也不出现在 `app.routes` 的遍历中（自定义挂载包装不
            # 展开嵌套路由）。**"取不到"不等于"没实现"**——因此它的可达性由一条真实的
            # WebSocket 连接测试证明（见 `test_agent_events_route.py`），而不是由这里
            # 的路由检查"推断"。
            return pairs
        finally:
            database.close()


def _expected() -> set[tuple[str, str]]:
    """读取签入的契约（仅 HTTP 对）。"""
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {
        (item["method"], item["path"])
        for item in data["pairs"]
        if item["method"] != "WS"
    }


def test_the_manifest_records_the_websocket_endpoint() -> None:
    """契约里记录了 WebSocket 端点。

    **它的可达性不在这里验证**——WS 不进 OpenAPI，也不出现在路由遍历中。**"取不到"
    不等于"没实现"**，因此那条断言放在真实连接的测试里（`test_agent_events_route.py`，
    14 项）。这里只确认它被记录下来了。
    """
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    websockets = [item for item in data["pairs"] if item["method"] == "WS"]

    assert websockets == [{"method": "WS", "path": "/v1/events"}]


def test_served_routes_match_the_recorded_contract(served_pairs: set[tuple[str, str]]) -> None:
    """**实际路由集合必须与签入的契约完全一致。**

    两个方向都要检查：少一条是能力缺失，多一条是未记录的协议变更。
    """
    expected = _expected()

    missing = expected - served_pairs
    extra = served_pairs - expected
    assert not missing, f"契约里有而实际没有: {sorted(missing)}"
    assert not extra, f"实际有而契约没有（未记录的协议变更）: {sorted(extra)}"


def test_the_manifest_records_methods_not_just_paths() -> None:
    """**清单必须含方法维度。**

    若清单退化成只有路径，`POST /v1/ledger/accounts` 那类"某条路径上少一个方法"的缺口
    会再次被漏掉——而它真实发生过。
    """
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    methods = {item["method"] for item in data["pairs"]}

    assert len(methods) >= 4, f"方法维度不足，实际只有: {sorted(methods)}"
    assert "POST" in methods and "GET" in methods and "DELETE" in methods


def test_accounts_supports_both_get_and_post() -> None:
    """**`/v1/ledger/accounts` 必须同时有 GET 与 POST。**

    这条单独固定那个曾真实缺失的方法（创建账户）。
    """
    assert ("GET", "/v1/ledger/accounts") in _expected()
    assert ("POST", "/v1/ledger/accounts") in _expected()
