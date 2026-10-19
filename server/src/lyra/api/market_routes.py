"""行情路由：设置与持仓估值。

对齐 TypeScript 侧的路径：

| 路由 | 说明 |
| --- | --- |
| ``GET /v1/market/settings`` | 读取**当前用户**的设置（含实际生效的路由） |
| ``PUT /v1/market/settings`` | 保存**当前用户**的设置（revision CAS） |
| ``GET /v1/market/positions`` | 当前用户在请求范围内持仓的参考估值 |
| ``GET /v1/market/history`` | 当前用户已持有证券的历史行情 |
| ``GET /v1/market/underlying-quote`` | 当前用户已持仓标的的正股成交与前收盘摘要 |

**设置按用户读写**：设置行按 ``user_id`` 独立（P0 契约 §3.12 点名本模块）。
保存后的路由**不写回任何共享运行时**——路由每次由 :meth:`MarketDataRuntime.settings_snapshot`
按当前用户读取并显式传给报价调用。把路由装进运行时（或写回共享服务）会让最后保存的
用户覆盖所有人的来源（计划 §4.4）。

**``routes`` 与 ``effectiveRoutes`` 同时返回**：前者是用户选的顺序，后者是剔除未配置
来源后**实际可用**的顺序。只返回前者会让用户看到"已选中 alpaca"而每次请求都静默
回退到备来源。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request

from ..application.app_error import ApplicationError
# 行情设置正文上限，与参考实现的 `bodyLimit` 一致。
# 该正文只含一个修订号与两组路由；允许超长等于允许把任意大的内容送进校验与日志。
_MAX_SETTINGS_BODY_BYTES = 2_048

from ..application.financial_tool_handlers import handle_market_positions
from ..application.market_data_service import MarketStockSummaryLookup
from ..domain.analytics.positions import aggregate_positions
from ..domain.analytics.types import AccountScope, PositionRow
from ..domain.market_data.history import MarketHistoryRange
from ..domain.market_data.types import StockInstrument, market_instrument_key
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
from ..infrastructure.market_data.identifiers import provider_instrument
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

    @router.get("/v1/market/history")
    def read_market_history(request: Request) -> Any:
        """读取一个已授权账户中指定现有持仓的历史行情。

        证券身份只从同一短事务里的当前账本快照推导；请求传入的 opaque symbolKey
        不会被用作上游 ticker。账本版本与用户来源设置 revision 检查完成并提交后，
        才会读取共享行情服务，避免网络 I/O 占用 DuckDB 事务。可选 ``target`` 只改变
        历史序列身份；即使读取标的股票，也必须先匹配该账户内的真实持仓。

        @param request: 已通过认证中间件的单次 HTTP 请求。
        @returns 协议 3 历史行情响应，bars 时间戳表示区间起点。
        @raises HTTPException: query 非法、账户无权访问或账本/设置版本已变化时返回错误。
        """
        query = _strict_market_history_query(request)
        public_account_id = query["scope"]
        connection = request.app.state.database.connection
        user_id = _current_user_id(request)
        runtime = request.app.state.market_runtime
        authorized = _authorized_market_position(
            connection=connection,
            user_id=user_id,
            runtime=runtime,
            account_id=public_account_id,
            symbol_key=query["symbol_key"],
            direction=query["direction"],
            ledger_version=query["ledger_version"],
            settings_revision=query["settings_revision"],
        )
        position = authorized.position
        account_currency = authorized.account_currency
        ledger_version = authorized.ledger_version
        market_settings = authorized.market_settings

        # 行情序列身份只从已匹配持仓的字段派生，target 不会跳过账户持仓授权。
        from ..application.financial_tool_handlers import _instrument_for

        target_identity_issue: str | None = None
        if query["target"] == "underlying":
            instrument, target_identity_issue = _underlying_stock_identity(position)
            sequence_asset_type = "stock"
        else:
            instrument = _instrument_for(position)
            sequence_asset_type = position.asset_type
        timeframe: str = _history_timeframe(query["range"])
        adjustment = "split" if sequence_asset_type == "stock" else "raw"
        expired = _history_expired(position.expiry, instrument)
        currency = account_currency
        instrument_key = (
            None if instrument is None else market_instrument_key(instrument)
        )
        if account_currency != "USD":
            lookup = _unavailable_history("currency_unsupported")
        elif target_identity_issue is not None:
            lookup = _unavailable_history(target_identity_issue)
        elif instrument is None:
            lookup = _unavailable_history("unsupported_instrument")
        else:
            if account_currency != instrument.currency:
                lookup = _unavailable_history("currency_unsupported")
            elif runtime is None:
                lookup = _unavailable_history("not_configured")
            else:
                asset_route = market_settings.routes.get(
                    sequence_asset_type, ()
                )
                lookup = runtime.service.read_history(
                    instrument, query["range"], asset_route
                )

        fetched_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        metadata = {
            "accountScope": {"kind": "account", "account": public_account_id},
            "ledgerVersion": ledger_version,
            "settingsRevision": market_settings.revision,
            "fetchedAt": fetched_at,
        }
        data = {
            "accountId": public_account_id,
            "symbolKey": query["symbol_key"],
            "direction": query["direction"],
            "target": query["target"],
            "positionAssetType": position.asset_type,
            "assetType": sequence_asset_type,
            "currency": currency,
            "instrumentKey": instrument_key,
            "range": query["range"],
            "timeframe": timeframe,
            "status": lookup.status,
            "issue": lookup.issue,
            "cached": lookup.cached,
            "providerId": lookup.provider_id,
            "feed": lookup.feed,
            "timing": {"kind": "delayed", "delayMinutes": 16},
            "adjustment": adjustment,
            "expired": expired,
            "cutoff": lookup.cutoff,
            "fetchedAt": lookup.fetched_at,
            "bars": [
                {
                    "timestamp": bar.timestamp,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
                for bar in lookup.bars
            ],
        }
        return to_jsonable({"metadata": metadata, "data": data})

    @router.get("/v1/market/underlying-quote")
    def read_underlying_quote(request: Request) -> Any:
        """读取当前授权账户真实持仓标的的正股最新成交和前收盘摘要。

        请求只携带账本已有 ``symbol_key`` 与方向。真实股票代码只从已授权快照的
        ``underlying`` 派生并校验；即使是期权或已到期期权，也只查询其正股。短事务
        提交后才调用共享行情服务，避免网络请求占住数据库连接。

        @param request: 已通过认证中间件的单次 HTTP 请求。
        @returns 协议 3 正股成交/前收盘响应；缺前收盘时保留有效最新成交价。
        @raises HTTPException: query 非法、账户无权访问或版本已变化时返回稳定错误。
        """
        query = _strict_underlying_quote_query(request)
        connection = request.app.state.database.connection
        user_id = _current_user_id(request)
        runtime = request.app.state.market_runtime
        authorized = _authorized_market_position(
            connection=connection,
            user_id=user_id,
            runtime=runtime,
            account_id=query["scope"],
            symbol_key=query["symbol_key"],
            direction=query["direction"],
            ledger_version=query["ledger_version"],
            settings_revision=query["settings_revision"],
        )
        position = authorized.position
        instrument, identity_issue = _underlying_stock_identity(position)
        instrument_key = (
            None if instrument is None else market_instrument_key(instrument)
        )
        if authorized.account_currency != "USD":
            lookup = _unavailable_stock_summary("currency_unsupported")
        elif identity_issue is not None:
            lookup = _unavailable_stock_summary(identity_issue)
        elif instrument is None:
            lookup = _unavailable_stock_summary("unsupported_instrument")
        elif runtime is None:
            lookup = _unavailable_stock_summary("not_configured")
        else:
            lookup = runtime.service.read_stock_summary(
                instrument, authorized.market_settings.routes.get("stock", ())
            )

        fetched_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        metadata = {
            "accountScope": {"kind": "account", "account": query["scope"]},
            "ledgerVersion": authorized.ledger_version,
            "settingsRevision": authorized.market_settings.revision,
            "fetchedAt": fetched_at,
        }
        data = {
            "accountId": query["scope"],
            "symbolKey": query["symbol_key"],
            "direction": query["direction"],
            "positionAssetType": position.asset_type,
            "assetType": "stock",
            "instrumentKey": instrument_key,
            "currency": authorized.account_currency,
            "status": lookup.status,
            "issue": lookup.issue,
            "cached": lookup.cached,
            "providerId": lookup.provider_id,
            "feed": lookup.feed,
            "timing": {"kind": "delayed", "delayMinutes": 15},
            "fetchedAt": lookup.fetched_at,
            "price": (
                None
                if lookup.price is None
                else {"value": lookup.price.value, "asOf": lookup.price.as_of}
            ),
            "previousClose": (
                None
                if lookup.previous_close is None
                else {
                    "value": lookup.previous_close.value,
                    "sessionDate": lookup.previous_close.session_date,
                }
            ),
            "changePercent": lookup.change_percent,
            "changeIssue": lookup.change_issue,
            "comparisonBasis": "previous_session_close",
            "adjustment": "split",
        }
        return to_jsonable({"metadata": metadata, "data": data})

    return router


@dataclass(frozen=True)
class _AuthorizedMarketPosition:
    """在一个账本/设置版本快照中精确匹配的授权持仓。"""

    position: PositionRow
    account_currency: str
    ledger_version: int
    market_settings: MarketDataRuntimeSettings


def _authorized_market_position(
    *,
    connection: duckdb.DuckDBPyConnection,
    user_id: str,
    runtime: MarketDataRuntime | None,
    account_id: str,
    symbol_key: str,
    direction: str,
    ledger_version: int,
    settings_revision: int,
) -> _AuthorizedMarketPosition:
    """验证版本与账户授权，并从同一短事务内取得唯一真实持仓。

    网络 I/O 不属于本 helper；事务在返回前提交，使调用方只能在授权快照通过后访问
    共享行情服务。

    @param connection: 当前请求共享数据库连接。
    @param user_id: 已认证用户 UUID。
    @param runtime: 行情运行时，用于读取该用户设置快照。
    @param account_id: 请求中的单账户 UUID。
    @param symbol_key: 账本已有的 opaque symbol key。
    @param direction: 持仓方向。
    @param ledger_version: 客户端观察到的账本版本。
    @param settings_revision: 客户端观察到的行情设置修订号。
    @returns 已授权持仓、账户币种和一致版本快照。
    @raises HTTPException: 账户无权访问、版本冲突或持仓不存在/不唯一时失败。
    """
    public_scope = AccountScope(kind="account", account=account_id)
    connection.execute("BEGIN TRANSACTION")
    try:
        resolved_scope = _resolve_market_scope(connection, user_id, public_scope)
        snapshot = DuckDbLedgerRepository(
            connection, user_id=user_id
        ).load_snapshot(resolved_scope)
        market_settings = _user_settings_snapshot(runtime, connection, user_id)
        version_row = connection.execute(
            "SELECT coalesce(max(version), 0) FROM ledger_versions WHERE user_id = CAST(? AS UUID)",
            [user_id],
        ).fetchone()
        current_version = int(version_row[0]) if version_row else 0
        if current_version != ledger_version:
            raise HTTPException(
                status_code=409, detail={"code": "MARKET_LEDGER_CHANGED"}
            )
        if market_settings.revision != settings_revision:
            raise HTTPException(
                status_code=409, detail={"code": "MARKET_SETTINGS_CHANGED"}
            )
        positions = aggregate_positions(snapshot.open_lots, resolved_scope)
        matching = tuple(
            position for position in positions
            if position.symbol_key == symbol_key and position.direction == direction
        )
        if not matching:
            raise HTTPException(
                status_code=404, detail={"code": "POSITION_NOT_FOUND"}
            )
        if len(matching) != 1:
            raise HTTPException(
                status_code=409, detail={"code": "POSITION_NOT_UNIQUE"}
            )
        position = matching[0]
        account = next(
            (
                item for item in snapshot.accounts
                if item.legacy_account_key == position.account
            ),
            None,
        )
        if account is None:
            raise HTTPException(
                status_code=500,
                detail={"code": "ACCOUNT_SNAPSHOT_INCONSISTENT"},
            )
        connection.execute("COMMIT")
        return _AuthorizedMarketPosition(
            position=position,
            account_currency=account.base_currency,
            ledger_version=current_version,
            market_settings=market_settings,
        )
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _underlying_stock_identity(
    position: PositionRow,
) -> tuple[StockInstrument | None, str | None]:
    """从真实持仓的权威标的字段推导并校验正股身份。

    正股只有在 ``underlying`` 缺失时才允许尝试账本 ``symbol_key``；期权只接受其
    ``underlying``。调用 provider 前校验可避免无效身份消费预算或生成伪有效键。

    @param position: 已从当前用户账户快照匹配出的真实持仓。
    @returns 股票身份与稳定失败原因；无失败原因时身份一定有效。
    """
    underlying = position.underlying
    if (
        (underlying is None or underlying == "")
        and position.asset_type == "stock"
    ):
        underlying = position.symbol_key
    if not isinstance(underlying, str) or underlying == "":
        return None, "unsupported_instrument"
    candidate = StockInstrument(symbol=underlying)
    validation = provider_instrument(candidate)
    if not validation.ok:
        return None, validation.reason
    return candidate, None


def _strict_underlying_quote_query(request: Request) -> dict[str, Any]:
    """校验最新正股摘要请求的单值 query 白名单与格式。

    @param request: 本次 HTTP 请求。
    @returns 规范化账户 UUID、持仓键、方向与两个非负版本号。
    @raises HTTPException: 缺失、重复、未知或格式非法时返回 400。
    """
    required = {
        "scope", "symbol_key", "direction", "ledger_version", "settings_revision"
    }
    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in required or key in values:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "行情摘要参数无效"},
            )
        values[key] = value
    if not required.issubset(values) or any(value == "" for value in values.values()):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_ARGUMENT", "message": "行情摘要参数无效"},
        )
    try:
        account_id = str(UUID(values["scope"]))
    except (ValueError, AttributeError) as error:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_ARGUMENT", "message": "scope 必须是单账户 UUID"},
        ) from error
    if (
        len(values["symbol_key"]) > 512
        or values["direction"] not in ("long", "short")
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_ARGUMENT", "message": "行情摘要参数无效"},
        )
    versions: list[int] = []
    for name in ("ledger_version", "settings_revision"):
        value = values[name]
        if len(value) > 19 or not re.fullmatch(r"[0-9]+", value):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "版本号必须是非负整数"},
            )
        parsed = int(value)
        if parsed > 9_223_372_036_854_775_807:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "版本号超出范围"},
            )
        versions.append(parsed)
    return {
        "scope": account_id,
        "symbol_key": values["symbol_key"],
        "direction": values["direction"],
        "ledger_version": versions[0],
        "settings_revision": versions[1],
    }


def _unavailable_stock_summary(issue: str) -> MarketStockSummaryLookup:
    """为身份、币种或运行时前置条件失败构造空摘要结果。"""
    fetched_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    return MarketStockSummaryLookup(
        status="unavailable", price=None, previous_close=None,
        change_percent=None, change_issue=None, issue=issue, cached=False,
        provider_id=None, feed=None, fetched_at=fetched_at,
    )


def _strict_market_history_query(request: Request) -> dict[str, Any]:
    """校验历史路由的严格 query 白名单、单值与参数取值。

    FastAPI 常规参数绑定会忽略未知参数，并对重复参数默认取其中一个；历史持仓请求
    要求查询身份明确且唯一，因此这里检查原始 query 项列表。

    @param request: 本次 HTTP 请求。
    @returns 规范化账户 UUID、持仓键、方向、range、target 与两个版本号；省略 target 时为 position。
    @raises HTTPException: 参数缺失、重复、未知或格式非法时返回 400。
    """
    required = {
        "scope", "symbol_key", "direction", "range",
        "ledger_version", "settings_revision",
    }
    allowed = required | {"target"}
    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in values:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "行情历史参数无效"},
            )
        values[key] = value
    if not required.issubset(values) or any(value == "" for value in values.values()):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_ARGUMENT", "message": "行情历史参数无效"},
        )
    try:
        account_id = str(UUID(values["scope"]))
    except (ValueError, AttributeError) as error:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_ARGUMENT", "message": "scope 必须是单账户 UUID"},
        ) from error
    direction = values["direction"]
    history_range = values["range"]
    target = values.get("target", "position")
    if len(values["symbol_key"]) > 512:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_ARGUMENT", "message": "symbol_key 过长"},
        )
    if (
        direction not in ("long", "short")
        or history_range not in ("1d", "1w", "1m", "3m")
        or target not in ("position", "underlying")
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_ARGUMENT", "message": "行情历史参数无效"},
        )
    version_texts = (values["ledger_version"], values["settings_revision"])
    if any(
        len(value) > 19 or not re.fullmatch(r"[0-9]+", value)
        for value in version_texts
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_ARGUMENT", "message": "版本号必须是非负整数"},
        )
    ledger_version, settings_revision = (int(value) for value in version_texts)
    max_version = 9_223_372_036_854_775_807
    if ledger_version > max_version or settings_revision > max_version:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_ARGUMENT", "message": "版本号超出范围"},
        )
    return {
        "scope": account_id,
        "symbol_key": values["symbol_key"],
        "direction": direction,
        "range": history_range,
        "target": target,
        "ledger_version": ledger_version,
        "settings_revision": settings_revision,
    }


def _history_timeframe(history_range: MarketHistoryRange) -> str:
    """将受限历史窗口映射到固定 provider 粒度。

    @param history_range: 协议允许的 range 枚举。
    @returns Alpaca 使用的 ``15Min``、``1Hour`` 或 ``1Day``。
    """
    return {"1d": "15Min", "1w": "1Hour", "1m": "1Day", "3m": "1Day"}[history_range]


def _history_expired(expiry: str | None, instrument: Any) -> bool:
    """按纽约民用日期判断期权历史是否锚定在到期后的过去区间。

    @param expiry: 持仓的 ISO 到期日；股票传 ``None``。
    @param instrument: 从账本派生的完整行情合约。
    @returns 当前纽约日期已晚于到期日时为 ``True``。
    """
    if instrument is None or getattr(instrument, "asset_type", None) != "option" or expiry is None:
        return False
    now_day = datetime.now(UTC).astimezone(ZoneInfo("America/New_York")).date()
    return now_day > datetime.fromisoformat(expiry).date()


def _unavailable_history(issue: str) -> Any:
    """构造不访问 provider 时使用的空历史结果。

    @param issue: 对客户端稳定公开的不可用原因码。
    @returns 无 bars 的行情服务查找结果。
    """
    from ..application.market_data_service import MarketHistoryLookup

    now = datetime.now(UTC)
    fetched_at = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    cutoff = (now.timestamp() * 1000 - 16 * 60_000)
    cutoff_at = datetime.fromtimestamp(cutoff / 1000, UTC).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    return MarketHistoryLookup(
        status="unavailable", bars=(), issue=issue, cached=False,
        provider_id=None, feed=None, fetched_at=fetched_at, cutoff=cutoff_at,
    )


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
