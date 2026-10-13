"""显式状态码契约测试。

**为什么值得单独一条**：状态码是协议契约的一部分，而它比字段名更隐蔽——
路由存在、字段正确、响应头齐全，客户端仍可能因为状态码不同而走错分支
（例如把 `201` 当失败，或把 `204` 当有内容去解析空 body）。

本测试把参考实现里**显式设置**的状态码集中固定，避免它们被"简化"成默认的 200。
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from lyra.api.agent_routes import create_agent_router
from lyra.api.auth_routes import create_auth_router
from lyra.api.gmail_routes import create_gmail_router
from lyra.api.import_routes import create_import_router
from lyra.api.ledger_routes import create_ledger_router
from lyra.api.market_routes import create_market_router


def _status_codes() -> dict[str, int]:
    """收集所有**显式非 200** 的路由状态码。"""
    codes: dict[str, int] = {}
    for factory in (
        create_agent_router, create_auth_router, create_gmail_router,
        create_import_router, create_ledger_router, create_market_router,
    ):
        for route in factory().routes:
            if not isinstance(route, APIRoute):
                continue
            if route.status_code and route.status_code != 200:
                for method in sorted(route.methods or ()):
                    codes[f"{method} {route.path}"] = route.status_code
    return codes


def test_creation_endpoints_return_201() -> None:
    """**创建资源的端点返回 201。**

    返回 200 会让客户端无法区分"新建了"与"返回了已有的"——对导入预览尤其重要，
    因为客户端需要知道后续的 confirmToken 是一次新的授权。
    """
    codes = _status_codes()

    assert codes["POST /v1/imports/preview"] == 201
    assert codes["POST /v1/agent/sessions"] == 201
    assert codes["POST /v1/google/authorization-transactions"] == 201
    assert codes["POST /v1/auth/{provider}/start"] == 201


def test_logout_returns_204_without_a_body() -> None:
    """**登出返回 204 且无正文。**

    返回带空 body 的 200 会让客户端去解析一个空字符串并因此报错。

    另：这条路由的返回注解**必须是 `None` 而不是 `Any`**——FastAPI 会把 `Any` 当作
    response_model，于是 204 配上响应模型会在**路由注册时**断言失败，导致整个应用
    构造不出来（那个错误的外观与登出毫无关系，排查成本很高）。
    """
    codes = _status_codes()

    assert codes["POST /v1/auth/logout"] == 204

    from inspect import signature

    from lyra.api.auth_routes import create_auth_router as factory

    logout = next(
        route for route in factory().routes
        if isinstance(route, APIRoute) and route.path == "/v1/auth/logout"
    )
    annotation = signature(logout.endpoint).return_annotation
    # 本模块用了 `from __future__ import annotations`，因此注解可能是字符串 `"None"`。
    # 两种形式都接受，但**不接受 `Any`**——那正是会让 204 断言失败的写法。
    assert annotation in (None, "None"), (
        f"204 路由的返回注解必须是 None，不能用 {annotation!r}"
    )


def test_explicit_status_codes_are_the_documented_five() -> None:
    """显式状态码是一个**闭集**：新增时必须显式决定，不能靠默认值。"""
    assert set(_status_codes().values()) == {201, 204}
