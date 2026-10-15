"""账本与分析的只读路由。

路径与 TypeScript 侧逐条对齐（``/v1/ledger/*`` 与 ``/v1/analytics/*``），
协议版本仍为 2。

**响应形状**：``{"metadata": {...}, "data": {...}}``，与参考实现逐字段一致。

``metadata`` 携带**服务端权威生成**的语境：账户范围、解析出的账户 ID、按币种
分组、账本版本、数据截止时间与获取时间。客户端用它判断"这份数据是什么时候、
在什么范围下算出来的"；让客户端自己拼这些字段，等于让它用请求参数覆盖服务端
事实。

**这一层曾整体缺失**：早期实现直接返回领域对象，于是 Flutter 解析
``json["metadata"]`` 时拿到 ``null``，总览恒显示"数据未能加载"——服务端 200、
客户端报错，而当时的测试只校验路径与状态码，看不见响应形状。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response

from ..application.account_service import create_account
from ..application.app_error import ApplicationError
from ..application.device_authenticator import DeviceAuthenticator
from ..application.financial_tool_handlers import handle_currency_grouped
from ..application.idempotent_mutation import validate_idempotency_key
from ..application.public_projection import (
    account_id_map,
    project_account_list,
    project_cashflow_query,
    project_currency_grouped,
    project_expiring,
    project_extreme_trades,
    project_ledger_audit,
    project_option_risk,
    project_positions,
    project_realized_query,
    project_trade_query,
)
from ..domain.analytics.audit import audit_ledger
from ..domain.analytics.currency_groups import group_by_currency
from ..domain.analytics.positions import aggregate_positions, analyze_expiring_positions
from ..domain.analytics.queries import (
    CashflowQueryFilters,
    RealizedQueryFilters,
    TradeQueryFilters,
    query_cashflow,
    query_realized,
    query_trades,
)
from ..domain.analytics.risk import calculate_option_risk
from ..domain.analytics.trade_analysis import (
    calculate_extreme_trades,
)
from ..domain.fifo.replay import replay_fifo
from ..domain.analytics.types import AccountScope
from ..domain.ledger.errors import IngestError
from ..infrastructure.db.account_lookup import resolve_legacy_account_key
from ..infrastructure.db.ledger_repository import DuckDbLedgerRepository
from .dependencies import parse_scope_query, require_device, to_jsonable

# oracle 录制分析层时使用的默认参数，与本模块的默认值保持一致。
DEFAULT_EXPIRING_DAYS = 30
DEFAULT_EXTREMES_LIMIT = 10


def create_ledger_router() -> APIRouter:
    """构造账本与分析路由。

    @returns 已挂载只读端点的路由器；全部端点要求设备认证。
    """
    router = APIRouter(dependencies=[Depends(require_device)])

    def _invalid_cursor(error: Any) -> HTTPException:
        """把游标错误翻译成 400。

        **不返回 500，也不回退到第一页**：游标是客户端可控输入，无效即请求错误。
        """
        return HTTPException(
            status_code=400,
            detail={"code": "INVALID_CURSOR", "message": str(error)},
        )

    def _resolved_scope(request: Request, scope: str | None) -> AccountScope:
        """解析 ``?scope=`` 并把它转成**账本用的账户键**。

        ``scope`` 参数是账户 **UUID**（与参考实现一致），而账本查询用的是**脱敏账户键**。
        两者类型相同，因此不做这一步转换**不会报错**——只会返回一个空账本。

        @raises HTTPException: 参数缺失或格式不符（400）。
        @raises HTTPException: 账户不存在（404）——不静默返回空账本。
        """
        parsed = parse_scope_query(scope)
        if parsed.kind == "all":
            return parsed
        key = resolve_legacy_account_key(
            request.app.state.database.connection, parsed.account
        )
        if key is None:
            # 账户不存在时**报错而不是返回空账本**：后者会让客户端以为"这个账户没有
            # 交易"，而实际上是它根本不存在。
            raise HTTPException(
                status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"}
            )
        return AccountScope(kind="account", account=key)

    def _snapshot(request: Request, scope: str | None) -> Any:
        """按账户范围读取账本快照。"""
        repository = DuckDbLedgerRepository(request.app.state.database.connection)
        return repository.load_snapshot(_resolved_scope(request, scope))

    def _scope(request: Request, scope: str | None) -> AccountScope:
        return _resolved_scope(request, scope)

    def _resolved(request: Request, scope: str | None) -> tuple[Any, AccountScope]:
        """一次请求内**只解析一次**快照与账户范围。

        元数据里的账户范围、账户 ID 与按币种分组必须与本次查询用的是同一份事实；
        各自重算会让两者在并发写入下出现分歧（元数据说 A 币种、数据是 B 币种）。
        """
        cached = getattr(request.state, "ledger_resolution", None)
        if cached is None:
            resolved = _resolved_scope(request, scope)
            cached = (_snapshot(request, scope), resolved)
            request.state.ledger_resolution = cached
        return cached

    def _envelope(request: Request, scope: str | None, data: Any) -> Any:
        """把查询结果包成 ``{metadata, data}``。

        @param data: 已投影（camelCase）的查询结果。
        @returns 可直接序列化的公开响应。
        """
        snapshot, resolved = _resolved(request, scope)
        connection = request.app.state.database.connection
        row = connection.execute(
            "SELECT coalesce(max(version), 0) FROM ledger_versions"
        ).fetchone()
        ledger_version = int(row[0]) if row else 0
        fetched_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        by_currency: dict[str, list[str]] = {}
        for account in snapshot.accounts:
            by_currency.setdefault(account.base_currency, []).append(account.id)
        metadata = {
            "accountScope": (
                {"kind": "all"} if resolved.kind == "all"
                else {"kind": "account", "account": resolved.account}
            ),
            "accountIds": [account.id for account in snapshot.accounts],
            "currencyGroups": [
                {"currency": currency, "accountIds": ids}
                for currency, ids in sorted(by_currency.items())
            ],
            "aggregateCurrency": (
                next(iter(by_currency)) if len(by_currency) == 1 else None
            ),
            "ledgerVersion": ledger_version,
            # 数据截止时间与获取时间都由服务端时钟给出：客户端不得用请求参数覆盖。
            "asOf": fetched_at,
            "fetchedAt": fetched_at,
        }
        return to_jsonable({"metadata": metadata, "data": data})

    @router.post("/v1/ledger/accounts", status_code=201)
    async def create_account_route(
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Any:
        """创建一个账户。

        正文为 ``{displayName, accountIdentifier, baseCurrency}``。

        **``accountIdentifier`` 是文件账户标识**（导入时的匹配键）：它落库但不入公开
        响应，对外只暴露末尾 3–4 位数字作为脱敏后缀。

        响应带 ``cache-control: no-store``（见 :mod:`error_envelope`）：创建账户的响应
        不应被缓存。

        **串行化由 DuckDB 事务提供**（:func:`persist_created_account` 内部开事务），
        与导入提交路径一致；``WriteCoordinator`` 在本实现里服务于异步操作，不是所有
        写入的必经之路。

        @param request: 请求对象。
        @param idempotency_key: 可选的幂等键。
        @returns 201 与 ``{account}``（或重放时为 200）。
        @raises HTTPException: 参数非法（400）、幂等键被复用（409）。
        """
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(
                status_code=400, detail={"code": "INVALID_ARGUMENT", "message": "账户创建参数无效"}
            ) from error

        if idempotency_key is not None:
            validate_idempotency_key(idempotency_key)

        connection = request.app.state.database.connection
        now = datetime.now(UTC)
        try:
            account, replayed = create_account(
                connection,
                body=body,
                device_id=request.state.device.id,
                request_id=getattr(request.state, "request_id", ""),
                idempotency_key=idempotency_key,
                now=now,
            )
        except ApplicationError as error:
            raise HTTPException(
                status_code=error.status_code,
                detail={"code": error.code, "message": error.message},
            ) from error

        if replayed:
            # **重放返回 200 而不是 201**：201 表示"本次创建了一个资源"，而重放没有
            # 创建任何东西。客户端据此判断是否需要刷新列表。
            response.status_code = 200
        public = project_account_list([account])[0]
        return {"account": public}

    @router.get("/v1/ledger/accounts")
    def read_accounts(request: Request, scope: str | None = Query(default=None)) -> Any:
        """当前设备的活跃与已归档账户；**不暴露券商账户的完整标识**。

        ``scope`` 与其它账本端点同义：账户清单本身不受范围过滤，但**元数据里的范围
        必须来自同一个显式参数**——省略它会让"查全部账户"变成隐式默认，而本项目的
        既定方向是"忘记带参数就报错，而不是给全部数据"。
        """
        connection = request.app.state.database.connection
        rows = connection.execute(
            """
            SELECT id, display_name, broker, masked_suffix, base_currency, status, created_at
            FROM accounts
            """
        ).fetchall()
        accounts = [
            {
                "id": str(row[0]), "display_name": row[1], "broker": row[2],
                "masked_suffix": row[3], "base_currency": row[4], "status": row[5],
                "created_at": row[6].astimezone(UTC)
                .isoformat(timespec="milliseconds").replace("+00:00", "Z")
                if hasattr(row[6], "astimezone") else str(row[6]),
            }
            for row in rows
        ]
        projected = project_account_list(accounts)
        return _envelope(request, scope, {"rows": projected, "count": len(projected)})

    @router.get("/v1/ledger/summary")
    def read_summary(request: Request, scope: str | None = Query(default=None)) -> Any:
        """账户总览；返回按币种分组的信封。"""
        snapshot, resolved = _resolved(request, scope)
        return _envelope(request, scope, handle_currency_grouped(
            "summary", snapshot, resolved, {}
        ))

    @router.get("/v1/ledger/positions")
    def read_positions(
        request: Request,
        scope: str | None = Query(default=None),
        underlying: str | None = Query(default=None),
    ) -> Any:
        """当前持仓。"""
        snapshot, resolved = _resolved(request, scope)
        rows = aggregate_positions(snapshot.open_lots, resolved, underlying)
        projected = project_positions(rows, account_id_map(snapshot.accounts))
        return _envelope(request, scope, {
            "rows": projected,
            "count": len(projected),
            "basis": "historical_cost_not_market_value",
        })

    @router.get("/v1/ledger/expiring")
    def read_expiring(
        request: Request,
        scope: str | None = Query(default=None),
        as_of: str = Query(...),
        days: int = Query(default=DEFAULT_EXPIRING_DAYS),
    ) -> Any:
        """到期窗口内的未平期权；``as_of`` 必填，避免结果随服务器日期漂移。"""
        snapshot, resolved = _resolved(request, scope)
        positions = aggregate_positions(snapshot.open_lots, resolved)
        result = analyze_expiring_positions(positions, as_of, days)
        return _envelope(request, scope, project_expiring(result, account_id_map(snapshot.accounts)))

    @router.get("/v1/ledger/trades")
    def read_trades(
        request: Request,
        scope: str | None = Query(default=None),
        cursor: str | None = Query(default=None),
    ) -> Any:
        """交易流水；公开字段不含哈希与来源引用。

        ``cursor`` 是上一次响应里的 ``nextCursor`` 原样回传。**无效游标报 400 而不是
        回退到第一页**——回退会让客户端把"游标坏了"当成"数据到头了"。
        """
        snapshot, resolved = _resolved(request, scope)
        try:
            result = query_trades(
                snapshot.transactions, resolved,
                TradeQueryFilters(cursor=cursor),
            )
        except IngestError as error:
            raise _invalid_cursor(error) from error
        return _envelope(request, scope, project_trade_query(
            result,
            account_id_map(snapshot.accounts),
            {t.transaction_hash: t for t in snapshot.transactions},
        ))

    @router.get("/v1/ledger/realized")
    def read_realized(
        request: Request,
        scope: str | None = Query(default=None),
        cursor: str | None = Query(default=None),
    ) -> Any:
        """已实现明细；隐藏开平仓交易哈希。"""
        snapshot, resolved = _resolved(request, scope)
        fifo = replay_fifo(snapshot.transactions)
        try:
            result = query_realized(
                fifo.realized_records, resolved, RealizedQueryFilters(cursor=cursor)
            )
        except IngestError as error:
            raise _invalid_cursor(error) from error
        return _envelope(request, scope, project_realized_query(
            result,
            account_id_map(snapshot.accounts),
        ))

    @router.get("/v1/ledger/cashflow")
    def read_cashflow(
        request: Request,
        scope: str | None = Query(default=None),
        cursor: str | None = Query(default=None),
    ) -> Any:
        """现金流水；附分动作合计。"""
        snapshot, resolved = _resolved(request, scope)
        try:
            result = query_cashflow(
                snapshot.transactions, resolved, CashflowQueryFilters(cursor=cursor)
            )
        except IngestError as error:
            raise _invalid_cursor(error) from error
        return _envelope(request, scope, project_cashflow_query(
            result,
            account_id_map(snapshot.accounts),
            {t.transaction_hash: t for t in snapshot.transactions},
        ))

    @router.get("/v1/ledger/audit")
    def read_audit(request: Request, scope: str | None = Query(default=None)) -> Any:
        """账本一致性审计（只读）。"""
        snapshot, resolved = _resolved(request, scope)
        fifo = replay_fifo(snapshot.transactions)
        return _envelope(request, scope, project_ledger_audit(
            audit_ledger(snapshot.transactions, fifo, resolved)
        ))

    @router.get("/v1/analytics/monthly")
    def read_monthly(request: Request, scope: str | None = Query(default=None)) -> Any:
        """月度绩效；返回按币种分组的信封。"""
        snapshot, resolved = _resolved(request, scope)
        return _envelope(request, scope, handle_currency_grouped(
                "monthly", snapshot, resolved, {}
            ))

    @router.get("/v1/analytics/allocation")
    def read_allocation(
        request: Request,
        scope: str | None = Query(default=None),
        group_by: str = Query(default="underlying"),
    ) -> Any:
        """历史成本配置。"""
        snapshot, resolved = _resolved(request, scope)
        return _envelope(request, scope, handle_currency_grouped(
                "allocation", snapshot, resolved, {"groupBy": group_by}
            ))

    @router.get("/v1/analytics/attribution")
    def read_attribution(
        request: Request,
        scope: str | None = Query(default=None),
        group_by: str = Query(default="underlying"),
    ) -> Any:
        """已实现损益归因。"""
        snapshot, resolved = _resolved(request, scope)
        return _envelope(request, scope, handle_currency_grouped(
                "attribution", snapshot, resolved, {"groupBy": group_by}
            ))

    @router.get("/v1/analytics/drawdown")
    def read_drawdown(request: Request, scope: str | None = Query(default=None)) -> Any:
        """日度回撤；返回按币种分组的信封。"""
        snapshot, resolved = _resolved(request, scope)
        return _envelope(request, scope, handle_currency_grouped(
                "drawdown", snapshot, resolved, {}
            ))

    @router.get("/v1/analytics/risk")
    def read_risk(
        request: Request,
        scope: str | None = Query(default=None),
        as_of: str = Query(...),
    ) -> Any:
        """期权风险桶；``as_of`` 必填。"""
        snapshot, resolved = _resolved(request, scope)

        def calculate(subset: Any) -> dict[str, Any]:
            # **这里刻意传全部而不是解析后的范围。**
            #
            # 参考实现传的也是 `{ kind: "all" }`（`financial-tool-runtime.ts:208`）：
            # `subset` 已由上方按范围筛过，风险计算自身只关心其内部的分组口径。
            # 改用解析后的范围会改变分组行为，与参考实现分叉。
            return project_option_risk(
                calculate_option_risk(subset.open_lots, AccountScope(kind="all"), as_of)
            )

        return _envelope(request, scope, project_currency_grouped(
            group_by_currency(snapshot, resolved, calculate)
        ))

    @router.get("/v1/analytics/holding-period")
    def read_holding_period(request: Request, scope: str | None = Query(default=None)) -> Any:
        """持有期汇总。"""
        snapshot, resolved = _resolved(request, scope)
        return _envelope(request, scope, handle_currency_grouped(
                "holding-period", snapshot, resolved, {}
            ))

    @router.get("/v1/analytics/extremes")
    def read_extremes(
        request: Request,
        scope: str | None = Query(default=None),
        limit: int = Query(default=DEFAULT_EXTREMES_LIMIT),
    ) -> Any:
        """最佳与最差交易。"""
        snapshot, resolved = _resolved(request, scope)
        fifo = replay_fifo(snapshot.transactions)
        return _envelope(request, scope, project_extreme_trades(
            calculate_extreme_trades(fifo.realized_records, resolved, limit),
            account_id_map(snapshot.accounts),
        ))

    @router.get("/v1/analytics/streaks")
    def read_streaks(request: Request, scope: str | None = Query(default=None)) -> Any:
        """最长连续盈利与亏损。"""
        snapshot, resolved = _resolved(request, scope)
        return _envelope(request, scope, handle_currency_grouped(
                "streaks", snapshot, resolved, {}
            ))

    @router.get("/v1/analytics/stress")
    def read_stress(request: Request, scope: str | None = Query(default=None)) -> Any:
        """长期权权利金归零压力。"""
        snapshot, resolved = _resolved(request, scope)
        return _envelope(request, scope, handle_currency_grouped(
                "stress", snapshot, resolved, {}
            ))

    return router



def build_authenticator(database: Any, now: Any = None) -> DeviceAuthenticator:
    """按应用持有的数据库构造认证器。"""
    from ..infrastructure.db.device_repository import DuckDbDeviceRepository

    return DeviceAuthenticator(DuckDbDeviceRepository(database.connection), now)
