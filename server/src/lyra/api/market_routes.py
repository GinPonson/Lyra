"""行情路由：设置与持仓估值。

对齐 TypeScript 侧的路径：

| 路由 | 说明 |
| --- | --- |
| ``GET /v1/market/settings`` | 读取设置（含**实际生效**的路由） |
| ``PUT /v1/market/settings`` | 保存设置（revision CAS） |
| ``GET /v1/market/positions`` | 当前持仓的参考估值 |

**``routes`` 与 ``effectiveRoutes`` 同时返回**：前者是用户选的顺序，后者是剔除未配置
来源后**实际可用**的顺序。只返回前者会让用户看到"已选中 alpaca"而每次请求都静默
回退到备来源。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..application.app_error import ApplicationError
# 行情设置正文上限，与参考实现的 `bodyLimit` 一致。
# 该正文只含一个修订号与两组路由；允许超长等于允许把任意大的内容送进校验与日志。
_MAX_SETTINGS_BODY_BYTES = 2_048

from ..application.financial_tool_handlers import handle_market_positions
from ..domain.analytics.types import AccountScope
from ..domain.market_data.settings import (
    effective_routes,
    normalize_routes,
)
from ..infrastructure.db.market_settings_repository import (
    load_market_settings,
    persist_market_settings,
)
from .dependencies import require_device, to_jsonable


def create_market_router() -> APIRouter:
    """构造行情路由。

    @returns 设置与持仓端点；全部要求设备认证。
    """
    router = APIRouter(dependencies=[Depends(require_device)])

    @router.get("/v1/market/settings")
    def read_settings(request: Request) -> Any:
        """读取行情设置。"""
        connection = request.app.state.database.connection
        stored = load_market_settings(connection)
        runtime = request.app.state.market_runtime
        configured = frozenset(runtime.provider_ids) if runtime is not None else frozenset()
        routes = stored.routes if stored is not None else (
            runtime.default_routes if runtime is not None else normalize_routes(None)
        )
        return _settings_payload(
            revision=0 if stored is None else stored.revision,
            routes=routes,
            configured=configured,
            providers=_provider_options(runtime),
            updated_at=None if stored is None else stored.updated_at,
        )

    @router.put("/v1/market/settings")
    async def update_settings(request: Request) -> Any:
        """保存行情设置。

        正文为 ``{"expectedRevision": N, "routes": {"stock": [...], "option": [...]}}``。

        **正文上限 2048 字节**（与参考实现的 ``bodyLimit`` 一致）。这个上限不是性能
        优化：该正文只含一个修订号与两组路由，**允许超长正文等于允许把任意大的内容
        送进校验与日志**。
        """
        # 先看声明长度——**在读正文之前拒绝**，避免先把它整个读进内存。
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > _MAX_SETTINGS_BODY_BYTES:
                    raise HTTPException(
                        status_code=400,
                        detail={"code": "INVALID_ARGUMENT", "message": "行情设置正文过大"},
                    )
            except ValueError as error:
                raise HTTPException(
                    status_code=400, detail={"code": "INVALID_ARGUMENT"}
                ) from error
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(
                status_code=400, detail={"code": "INVALID_ARGUMENT"}
            ) from error
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT"})
        expected_revision = body.get("expectedRevision")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT"})
        try:
            routes = normalize_routes(body.get("routes"))
        except ValueError as error:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": str(error)},
            ) from error

        connection = request.app.state.database.connection
        try:
            revision = persist_market_settings(
                connection, expected_revision=expected_revision, routes=routes,
                now=datetime.now(UTC),
            )
        except ApplicationError as error:
            raise HTTPException(
                status_code=error.status_code, detail={"code": error.code, "message": error.message}
            ) from error

        runtime = request.app.state.market_runtime
        if runtime is not None:
            # **热切换**：设置保存后立刻生效，不需要重启。
            runtime.service.set_routes(routes)
        stored = load_market_settings(connection)
        return _settings_payload(
            revision=revision,
            routes=routes,
            configured=frozenset(runtime.provider_ids) if runtime is not None else frozenset(),
            providers=_provider_options(runtime),
            updated_at=None if stored is None else stored.updated_at,
        )

    @router.get("/v1/market/positions")
    def read_market_positions(
        request: Request,
        account: str | None = None,
        underlying: str | None = None,
    ) -> Any:
        """当前持仓的参考估值。

        **行情不可用不影响本端点的可用性**：行情只有可用时才附加到行上，
        账本数据照常返回。
        """
        connection = request.app.state.database.connection
        scope = AccountScope(kind="all") if not account else AccountScope(
            kind="account", account=account
        )
        return to_jsonable(handle_market_positions(
            connection, scope, request.app.state.market_runtime, underlying
        ))

    return router


def _provider_options(runtime: Any) -> list[dict[str, Any]]:
    """可选来源列表。"""
    if runtime is None:
        return []
    return [
        {"id": provider_id, "displayName": provider_id, "configured": True}
        for provider_id in runtime.provider_ids
    ]


def _settings_payload(
    *,
    revision: int,
    routes: Any,
    configured: frozenset[str],
    providers: list[dict[str, Any]],
    updated_at: str | None,
) -> dict[str, Any]:
    """构造设置响应。

    **``routes`` 与 ``effectiveRoutes`` 同时返回**：只返回前者会让用户看到"已选中
    alpaca"而每次请求都静默回退到备来源。
    """
    return {
        "revision": revision,
        "routes": {asset_type: list(values) for asset_type, values in routes.items()},
        "effectiveRoutes": {
            asset_type: list(values)
            for asset_type, values in effective_routes(routes, configured).items()
        },
        "providers": providers,
        "updatedAt": updated_at,
    }
