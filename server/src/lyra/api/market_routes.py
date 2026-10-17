"""行情路由：设置与持仓估值。

对齐 TypeScript 侧的路径：

| 路由 | 说明 |
| --- | --- |
| ``GET /v1/market/settings`` | 读取**当前用户**的设置（含实际生效的路由） |
| ``PUT /v1/market/settings`` | 保存**当前用户**的设置（revision CAS） |
| ``GET /v1/market/positions`` | 当前用户在请求范围内持仓的参考估值 |

**设置按用户读写**：设置行按 ``user_id`` 独立（P0 契约 §3.12 点名本模块）。
保存后的路由**不写回任何共享运行时**——路由每次由 :meth:`MarketDataRuntime.settings_snapshot`
按当前用户读取并显式传给报价调用。把路由装进运行时（或写回共享服务）会让最后保存的
用户覆盖所有人的来源（计划 §4.4）。

**``routes`` 与 ``effectiveRoutes`` 同时返回**：前者是用户选的顺序，后者是剔除未配置
来源后**实际可用**的顺序。只返回前者会让用户看到"已选中 alpaca"而每次请求都静默
回退到备来源。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import duckdb
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
from ..infrastructure.market_data.runtime import (
    MarketDataRuntime,
    MarketDataRuntimeSettings,
)
from .dependencies import parse_scope_query, require_user, to_jsonable


def create_market_router() -> APIRouter:
    """构造行情路由。

    @returns 设置与持仓端点；全部要求用户认证。
    """
    router = APIRouter(dependencies=[Depends(require_user)])

    @router.get("/v1/market/settings")
    def read_settings(request: Request) -> Any:
        """读取**当前用户**的行情设置。

        运行时不再持有"当前设置"，因此本端点只做一次按用户的读取；同一进程里的其它
        用户并发读写不会改变这里看到的结果。
        """
        connection = request.app.state.database.connection
        user_id = _current_user_id(request)
        runtime = request.app.state.market_runtime
        snapshot = _user_settings_snapshot(runtime, connection, user_id)
        return _settings_payload(
            revision=snapshot.revision,
            routes=snapshot.routes,
            configured=_configured_providers(runtime),
            providers=_provider_options(runtime),
            updated_at=snapshot.updated_at,
        )

    @router.put("/v1/market/settings")
    async def update_settings(request: Request) -> Any:
        """保存**当前用户**的行情设置。

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
        user_id = _current_user_id(request)
        try:
            persist_market_settings(
                connection, user_id,
                expected_revision=expected_revision, routes=routes,
                now=datetime.now(UTC),
            )
        except ApplicationError as error:
            raise HTTPException(
                status_code=error.status_code, detail={"code": error.code, "message": error.message}
            ) from error

        # 回读该用户**已提交**的设置：若有并发保存抢在前面，响应给出的是数据库里实际
        # 生效的版本，而不是本次请求自认为的那个。
        runtime = request.app.state.market_runtime
        snapshot = _user_settings_snapshot(runtime, connection, user_id)
        return _settings_payload(
            revision=snapshot.revision,
            routes=snapshot.routes,
            configured=_configured_providers(runtime),
            providers=_provider_options(runtime),
            updated_at=snapshot.updated_at,
        )

    @router.get("/v1/market/positions")
    def read_market_positions(
        request: Request,
        scope: str | None = None,
        underlying: str | None = None,
    ) -> Any:
        """按显式账户范围读取**当前用户**持仓的参考估值。

        **行情不可用不影响本端点的可用性**：行情只有可用时才附加到行上，
        账本数据照常返回。
        """
        connection = request.app.state.database.connection
        user_id = _current_user_id(request)
        public_scope = parse_scope_query(scope)
        runtime = request.app.state.market_runtime
        # 账本、修订号和路由必须来自同一数据库读快照；显式事务同时阻止共享连接被
        # 其它请求插入写操作。行情 I/O 在提交后进行，避免网络延迟占住数据库锁。
        connection.execute("BEGIN TRANSACTION")
        try:
            resolved_scope = _resolve_market_scope(connection, user_id, public_scope)
            snapshot = DuckDbLedgerRepository(
                connection, user_id=user_id
            ).load_snapshot(resolved_scope)
            # 设置快照按当前用户读取，且与账本读在同一个短事务里；快照只用于本次请求，
            # 不写回运行时，因此并发用户之间不会互相覆盖来源。
            market_settings = _user_settings_snapshot(runtime, connection, user_id)
            version_row = connection.execute(
                "SELECT coalesce(max(version), 0) FROM ledger_versions WHERE user_id = CAST(? AS UUID)",
                [user_id],
            ).fetchone()
            ledger_version = int(version_row[0]) if version_row else 0
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

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


def _current_user_id(request: Request) -> str:
    """取出本次请求已认证用户的 UUID 文本。

    **只认 :class:`AuthContext`**：路由级依赖 :func:`require_user` 已把不可变的认证
    上下文写进 ``request.state.auth``。设置与账户范围一律以它为准，任何请求参数都不得
    改写归属（P0 契约 §3.8）。

    @param request: 请求对象。
    @returns 当前用户 UUID 文本。
    """
    return request.state.auth.user_id


def _configured_providers(runtime: MarketDataRuntime | None) -> frozenset[str]:
    """返回凭据齐全的来源集合；未装配运行时为空集。"""
    return frozenset(runtime.provider_ids) if runtime is not None else frozenset()


def _user_settings_snapshot(
    runtime: MarketDataRuntime | None,
    connection: duckdb.DuckDBPyConnection,
    user_id: str,
) -> MarketDataRuntimeSettings:
    """读取当前用户生效的路由与修订号。

    正常运行期运行时总是存在，走运行时的按用户快照；运行时尚不存在时（未装配）直接
    读该用户的持久化行，保证同一份契约，**不退回全库或默认 owner**。

    @param runtime: 行情运行时；``None`` 表示未装配。
    @param connection: 数据库连接。
    @param user_id: 当前用户 UUID 文本。
    @returns 该用户的路由、修订号与保存时间。
    """
    if runtime is not None:
        return runtime.settings_snapshot(connection, user_id)
    stored = load_market_settings(connection, user_id)
    if stored is None:
        return MarketDataRuntimeSettings(
            routes=normalize_routes(None), revision=0, updated_at=None
        )
    return MarketDataRuntimeSettings(
        routes=stored.routes, revision=stored.revision, updated_at=stored.updated_at
    )


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


def _resolve_market_scope(
    connection: Any, user_id: str, scope: AccountScope
) -> AccountScope:
    """把公开账户 UUID 映射到账本查询使用的 legacy key。

    **映射带用户范围**（P0 契约 §3.11）：不带范围时 A 传入 B 的账户 UUID 会解析出
    B 的 legacy key，随后的账本查询就会返回 B 的数据。

    @param connection: 当前数据库连接。
    @param user_id: 当前用户 UUID 文本。
    @param scope: 已由 parse_scope_query 校验的公开账户范围。
    @returns 账本查询范围。
    @raises HTTPException: UUID 不属于当前用户或不存在时返回 404（两者不可区分）。
    """
    if scope.kind == "all":
        return scope
    key = resolve_legacy_account_key(connection, user_id, scope.account or "")
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
