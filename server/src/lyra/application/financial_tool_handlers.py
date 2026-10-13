"""分析命令的共享处理器。

路由与 Agent 工具**共用同一份实现**。若两侧各写一套，账户范围处理、投影字段与错误
映射都会各自漂移，而 M3 建立的 oracle 一致性只覆盖其中一份。

本模块先抽取形状相同的一组：**按币种分组信合**。这类命令的流程完全一致：

```
按账户范围取快照 → 按币种切分 → 每组独立计算 → 投影为 camelCase → 套信封
```

差异只在「算什么」与「怎么投影」两处，因此用一张表描述，而不是复制七遍。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..domain.analytics.audit import audit_ledger
from ..domain.analytics.currency_groups import group_by_currency
from ..domain.analytics.performance import (
    calculate_allocation,
    calculate_attribution,
    calculate_monthly_performance,
)
from ..domain.analytics.positions import aggregate_positions
from ..domain.analytics.risk import calculate_drawdown, calculate_option_risk
from ..domain.analytics.summary import calculate_account_summary
from ..domain.analytics.trade_analysis import (
    calculate_holding_period,
    calculate_option_stress,
    calculate_streaks,
)
from ..domain.analytics.types import AccountScope
from ..domain.fifo.replay import replay_fifo
from ..infrastructure.db.ledger_repository import LedgerSnapshot
from .public_projection import (
    project_allocation,
    project_attribution,
    project_drawdown,
    project_holding_period,
    project_ledger_audit,
    project_monthly,
    project_option_risk,
    project_option_stress,
    project_streaks,
    project_summary,
)

# 分组内部一律用「全部账户」：子集已经按币种切好，再套账户范围会重复过滤。
_INNER_SCOPE = AccountScope(kind="all")


@dataclass(frozen=True)
class CurrencyGroupedCommand:
    """一条按币种分组的分析命令。

    @param compute: 对**单一币种子集**执行计算，返回领域结果。
    @param project: 把领域结果投影为公开 camelCase 字典。
    """

    compute: Callable[[LedgerSnapshot, Mapping[str, Any]], Any]
    project: Callable[[Any], dict[str, Any]]


CURRENCY_GROUPED_COMMANDS: dict[str, CurrencyGroupedCommand] = {
    "summary": CurrencyGroupedCommand(
        compute=lambda subset, arguments: calculate_account_summary(
            subset.transactions, replay_fifo(subset.transactions), _INNER_SCOPE
        ),
        project=project_summary,
    ),
    "monthly": CurrencyGroupedCommand(
        compute=lambda subset, arguments: calculate_monthly_performance(
            replay_fifo(subset.transactions).realized_records, _INNER_SCOPE
        ),
        project=project_monthly,
    ),
    "allocation": CurrencyGroupedCommand(
        compute=lambda subset, arguments: calculate_allocation(
            subset.open_lots, _INNER_SCOPE, arguments.get("groupBy", "underlying")
        ),
        project=project_allocation,
    ),
    "attribution": CurrencyGroupedCommand(
        compute=lambda subset, arguments: calculate_attribution(
            replay_fifo(subset.transactions).realized_records,
            _INNER_SCOPE,
            arguments.get("groupBy", "underlying"),
        ),
        project=project_attribution,
    ),
    "drawdown": CurrencyGroupedCommand(
        compute=lambda subset, arguments: calculate_drawdown(
            replay_fifo(subset.transactions).realized_records, _INNER_SCOPE
        ),
        project=project_drawdown,
    ),
    "holding-period": CurrencyGroupedCommand(
        compute=lambda subset, arguments: calculate_holding_period(
            replay_fifo(subset.transactions).realized_records, _INNER_SCOPE
        ),
        project=project_holding_period,
    ),
    "streaks": CurrencyGroupedCommand(
        compute=lambda subset, arguments: calculate_streaks(
            replay_fifo(subset.transactions).realized_records, _INNER_SCOPE
        ),
        project=project_streaks,
    ),
    "risk": CurrencyGroupedCommand(
        # risk 与其余命令同形，只是多一个**必填** asOf：缺省成"今天"会让同一请求的
        # 结果随服务器日期漂移。
        compute=lambda subset, arguments: calculate_option_risk(
            subset.open_lots, _INNER_SCOPE, arguments["asOf"]
        ),
        project=project_option_risk,
    ),
    "stress": CurrencyGroupedCommand(
        compute=lambda subset, arguments: calculate_option_stress(
            subset.open_lots, _INNER_SCOPE
        ),
        project=project_option_stress,
    ),
}


def handle_currency_grouped(
    command: str,
    snapshot: LedgerSnapshot,
    account_scope: AccountScope,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """执行一条按币种分组的分析命令。

    @param command: 命令名，须在 :data:`CURRENCY_GROUPED_COMMANDS` 中。
    @param snapshot: 全量账本快照。
    @param account_scope: 已解析的账户范围。
    @param arguments: 已校验的参数（仅用于取 ``groupBy`` 等可选字段）。
    @returns 公开的币种信封。
    @raises KeyError: 命令未注册。
    """
    spec = CURRENCY_GROUPED_COMMANDS[command]
    grouped = group_by_currency(
        snapshot,
        account_scope,
        lambda subset: spec.project(spec.compute(subset, arguments)),
    )
    return {
        "aggregateCurrency": grouped.aggregate_currency,
        "groups": [
            {
                "currency": group.currency,
                "accountIds": list(group.account_ids),
                "data": group.data,
            }
            for group in grouped.groups
        ],
    }


# ---------------------------------------------------------------------------
# 详情类命令：不套币种信封，返回自身的 rows/count 形状
# ---------------------------------------------------------------------------


def handle_ledger_detail(
    command: str,
    snapshot: LedgerSnapshot,
    account_scope: AccountScope,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """执行一条账本/分析详情命令。

    这些命令的差异在「算什么」与「行怎么投影」，但都需要**账户键 → UUID 映射**
    （公开响应里账户以 UUID 出现）与部分命令需要的**交易哈希索引**。

    @param command: 命令名。
    @param snapshot: 全量账本快照。
    @param account_scope: 已解析的账户范围。
    @param arguments: 已校验的参数。
    @returns 公开响应。
    @raises KeyError: 命令未注册。
    """
    from ..domain.analytics.positions import (
        aggregate_positions,
        analyze_expiring_positions,
    )
    from ..domain.analytics.queries import query_cashflow, query_realized, query_trades
    from ..domain.analytics.trade_analysis import calculate_extreme_trades
    from .public_projection import (
        account_id_map,
        project_cashflow_query,
        project_expiring,
        project_extreme_trades,
        project_positions,
        project_realized_query,
        project_trade_query,
    )

    if command not in LEDGER_DETAIL_COMMANDS:
        raise KeyError(f"未注册的详情命令: {command}")
    ids = account_id_map(snapshot.accounts)
    by_hash = {t.transaction_hash: t for t in snapshot.transactions}

    if command == "positions":
        rows = aggregate_positions(
            snapshot.open_lots, account_scope, arguments.get("underlying")
        )
        projected = project_positions(rows, ids)
        return to_jsonable_result({
            "rows": projected,
            "count": len(projected),
            "basis": "historical_cost_not_market_value",
        })
    if command == "expiring":
        positions = aggregate_positions(snapshot.open_lots, account_scope)
        result = analyze_expiring_positions(
            positions, arguments["asOf"], arguments.get("days", 30)
        )
        return to_jsonable_result(project_expiring(result, ids))
    if command == "trades":
        return to_jsonable_result(project_trade_query(
            query_trades(snapshot.transactions, account_scope), ids, by_hash
        ))
    if command == "realized":
        return to_jsonable_result(project_realized_query(
            query_realized(replay_fifo(snapshot.transactions).realized_records, account_scope),
            ids,
        ))
    if command == "cashflow":
        return to_jsonable_result(project_cashflow_query(
            query_cashflow(snapshot.transactions, account_scope), ids, by_hash
        ))
    if command == "extremes":
        return to_jsonable_result(project_extreme_trades(
            calculate_extreme_trades(
                replay_fifo(snapshot.transactions).realized_records,
                account_scope,
                arguments.get("limit", 10),
            ),
            ids,
        ))
    # audit
    return to_jsonable_result(project_ledger_audit(
        audit_ledger(
            snapshot.transactions, replay_fifo(snapshot.transactions), account_scope
        )
    ))


# 详情类命令清单；与 :func:`handle_ledger_detail` 的分支一一对应。
LEDGER_DETAIL_COMMANDS: frozenset[str] = frozenset(
    {"positions", "expiring", "trades", "realized", "cashflow", "audit", "extremes"}
)


def to_jsonable_result(value: Any) -> Any:
    """把领域容器里的 Decimal 递归转为十进制字符串。

    与 API 层同义；放在这里是为了让工具结果与 HTTP 响应**共用同一条转换规则**。

    @param value: 领域结构。
    @returns 可 JSON 序列化的结构。
    """
    from decimal import Decimal

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: to_jsonable_result(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable_result(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# 持仓估值
# ---------------------------------------------------------------------------


def handle_market_positions(
    connection: Any,
    scope: AccountScope,
    market_runtime: Any,
    underlying: str | None = None,
) -> dict[str, Any]:
    """当前持仓的参考估值。

    路由与 Agent 工具**共用这一份实现**：各写一份会让字段与口径分叉，而两处的使用者
    都以为自己在看同一份数据。

    **行情不可用不影响本函数可用性**：行情只有可用时才附加到行上，账本数据照常返回。

    @param connection: 数据库连接。
    @param scope: 账户范围。
    @param market_runtime: 行情运行时；``None`` 表示未装配。
    @param underlying: 仅看某个标的。
    @returns 含 rows / count / pricedCount / complete / basis / marketData 的字典。
    """
    from ..domain.market_data.valuation import value_position
    from ..infrastructure.db.ledger_repository import DuckDbLedgerRepository
    from .public_projection import account_id_map, project_market_positions

    snapshot = DuckDbLedgerRepository(connection).load_snapshot(scope)
    positions = aggregate_positions(snapshot.open_lots, scope, underlying)

    rows: list[dict[str, Any]] = []
    for position in positions:
        instrument = _instrument_for(position)
        if market_runtime is None or instrument is None:
            valuation = value_position(
                quantity=str(position.qty), cost=str(position.cost),
                reference_price=None,
                asset_type="option" if position.asset_type == "option" else "stock",
                direction=position.direction,
            )
            lookup = None
        else:
            lookup = market_runtime.service.get_quote(instrument)
            reference = (
                None if lookup.reference_price is None else lookup.reference_price.value
            )
            valuation = value_position(
                quantity=str(position.qty), cost=str(position.cost),
                reference_price=reference,
                asset_type="option" if position.asset_type == "option" else "stock",
                direction=position.direction,
            )
        rows.append({
            "account": position.account,
            "symbol_key": position.symbol_key,
            "asset_type": position.asset_type,
            "underlying": position.underlying,
            "expiry": position.expiry,
            "strike": position.strike,
            "option_type": position.option_type,
            "direction": position.direction,
            "quantity": str(position.qty),
            "cost": str(position.cost),
            "first_open_date": position.first_open_date,
            "valuation": {
                "marketValue": valuation.market_value,
                "unrealizedPnl": valuation.unrealized_pnl,
                "multiplier": valuation.multiplier,
                "basis": valuation.basis,
            },
            "quote": (
                None if lookup is None
                else {
                    "status": lookup.status,
                    "issue": lookup.issue,
                    "asOf": (
                        None if lookup.reference_price is None
                        else lookup.reference_price.as_of
                    ),
                }
            ),
        })

    return project_market_positions(
        rows, market_runtime, account_id_map(snapshot.accounts)
    )


def _instrument_for(position: Any) -> Any:
    """把持仓行转成行情合约；无法翻译时返回 ``None``。

    @returns 股票或期权合约；不支持的身份返回 ``None``。
    """
    from ..domain.market_data.types import OptionInstrument, StockInstrument

    if position.asset_type == "stock":
        if not isinstance(position.underlying, str):
            return None
        return StockInstrument(symbol=position.underlying)
    if (
        not isinstance(position.underlying, str)
        or not isinstance(position.expiry, str)
        or position.strike is None
        or position.option_type not in ("C", "P")
    ):
        return None
    return OptionInstrument(
        underlying=position.underlying, expiry=position.expiry,
        strike=str(position.strike), option_type=position.option_type,
    )


def _market_status(market_runtime: Any) -> dict[str, Any]:
    """行情来源状态。"""
    if market_runtime is None:
        return {"routes": {"stock": [], "option": []}, "providers": []}
    return {
        "routes": {
            asset_type: list(providers)
            for asset_type, providers in market_runtime.default_routes.items()
        },
        "providers": [
            {"id": provider_id, "configured": True}
            for provider_id in market_runtime.provider_ids
        ],
    }
