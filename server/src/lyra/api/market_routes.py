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
from ..infrastructure.db.account_lookup import resolve_legacy_account_key
from ..infrastructure.db.ledger_repository import DuckDbLedgerRepository
from ..infrastructure.db.market_settings_repository import (
    load_market_settings,
    persist_market_settings,
)
from .dependencies import parse_scope_query, require_device, to_jsonable


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
        revision = 0 if stored is None else stored.revision
        if runtime is not None:
            # 读取时同步持久化状态，避免服务重启或外部更新后继续按旧路由报价。
            current = runtime.initialize_settings(routes, revision)
            routes = current.routes
            revision = current.revision
        return _settings_payload(
            revision=revision,
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
        stored = load_market_settings(connection)
        active_routes = routes
        active_revision = revision
        if runtime is not None:
            # **热切换**：写入后的持久化版本立即生效；并发较新的版本优先。
            current = runtime.initialize_settings(
                routes if stored is None else stored.routes,
                revision if stored is None else stored.revision,
            )
            active_routes = current.routes
            active_revision = current.revision
        return _settings_payload(
            revision=active_revision,
            routes=active_routes,
            configured=frozenset(runtime.provider_ids) if runtime is not None else frozenset(),
            providers=_provider_options(runtime),
            updated_at=None if stored is None else stored.updated_at,
        )

    @router.get("/v1/market/positions")
    def read_market_positions(
        request: Request,
        scope: str | None = None,
        underlying: str | None = None,
    ) -> Any:
        """按显式账户范围读取持仓参考估值。

        **行情不可用不影响本端点的可用性**：行情只有可用时才附加到行上，
        账本数据照常返回。
        """
        connection = request.app.state.database.connection
        public_scope = parse_scope_query(scope)
        # 账本、修订号和路由必须来自同一数据库读快照；显式事务同时阻止共享连接被
        # 其它请求插入写操作。行情 I/O 在提交后进行，避免网络延迟占住数据库锁。
        connection.execute("BEGIN TRANSACTION")
        try:
            resolved_scope = _resolve_market_scope(connection, public_scope)
            snapshot = DuckDbLedgerRepository(connection).load_snapshot(resolved_scope)
            stored = load_market_settings(connection)
            version_row = connection.execute(
                "SELECT coalesce(max(version), 0) FROM ledger_versions"
            ).fetchone()
            ledger_version = int(version_row[0]) if version_row else 0
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

        runtime = request.app.state.market_runtime
        market_settings = None
        if runtime is not None:
            routes = stored.routes if stored is not None else runtime.default_routes
            revision = 0 if stored is None else stored.revision
            market_settings = runtime.initialize_settings(routes, revision)
        data = handle_market_positions(
            connection,
            resolved_scope,
            runtime,
            underlying,
            snapshot=snapshot,
            market_settings=market_settings,
        )
        return _market_positions_envelope(
            public_scope, snapshot, data, ledger_version
        )

    return router


def _provider_options(runtime: Any) -> list[dict[str, Any]]:
    """投影真实来源目录，保留未配置来源的不可用状态。"""
    if runtime is None:
        return []
    return [
        {
            "id": provider.id,
            "displayName": provider.display_name,
            "assetTypes": list(provider.asset_types),
            "configured": provider.configured,
            "timing": {
                "kind": provider.timing_kind,
                "delayMinutes": provider.delay_minutes,
            },
            "cacheTtlSeconds": provider.cache_ttl_seconds,
        }
        for provider in runtime.provider_options
    ]


def _resolve_market_scope(connection: Any, scope: AccountScope) -> AccountScope:
    """把公开账户 UUID 映射到账本查询使用的 legacy key。

    @param connection: 当前数据库连接。
    @param scope: 已由 parse_scope_query 校验的公开账户范围。
    @returns 账本查询范围。
    @raises HTTPException: UUID 对应账户不存在时返回 404。
    """
    if scope.kind == "all":
        return scope
    key = resolve_legacy_account_key(connection, scope.account or "")
    if key is None:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    return AccountScope(kind="account", account=key)


def _market_positions_envelope(
    scope: AccountScope,
    snapshot: Any,
    data: dict[str, Any],
    ledger_version: int,
) -> dict[str, Any]:
    """为行情持仓数据添加与账本查询一致的 metadata/data 信封。

    @param scope: 公开 UUID 范围；不含账本内部 legacy key。
    @param snapshot: 本次数据使用的账本快照。
    @param data: 共享持仓估值处理器返回的数据部分。
    @param ledger_version: 与快照在同一只读事务中捕获的账本版本。
    @returns 可直接序列化的公开响应。
    """
    fetched_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    accounts_by_currency: dict[str, list[str]] = {}
    for account in snapshot.accounts:
        accounts_by_currency.setdefault(account.base_currency, []).append(account.id)
    metadata = {
        "accountScope": (
            {"kind": "all"}
            if scope.kind == "all"
            else {"kind": "account", "account": scope.account}
        ),
        "accountIds": [account.id for account in snapshot.accounts],
        "currencyGroups": [
            {"currency": currency, "accountIds": ids}
            for currency, ids in sorted(accounts_by_currency.items())
        ],
        "aggregateCurrency": (
            next(iter(accounts_by_currency))
            if len(accounts_by_currency) == 1
            else None
        ),
        "ledgerVersion": ledger_version,
        "asOf": fetched_at,
        "fetchedAt": fetched_at,
    }
    return to_jsonable({"metadata": metadata, "data": data})


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
