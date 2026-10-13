"""财务工具的 `ToolDefinition` 装配。

把工具名、参数 schema 与处理器绑成 :class:`ToolDefinition`，供 Agent 与 REST 复用。
处理器一律调用 :mod:`financial_tool_handlers` 的共享实现——**不在这里重写分析逻辑**，
否则路由与 Agent 会各有一份，字段与口径必然分叉。

本模块绑定全部 **18 个工具**（7 条币种信封命令 + 7 条详情命令 + `accounts` +
`market-positions`）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import duckdb

from ..domain.analytics.types import AccountScope
from ..infrastructure.db.ledger_repository import DuckDbLedgerRepository
from .financial_tool_handlers import (
    CURRENCY_GROUPED_COMMANDS,
    LEDGER_DETAIL_COMMANDS,
    handle_currency_grouped,
    handle_ledger_detail,
    handle_market_positions,
)
from .public_projection import project_account_list
from .tool_runtime import ToolDefinition

_ACCOUNT_PROPERTY = {"type": "string", "minLength": 1}
_GROUP_BY_PROPERTY = {"type": "string", "enum": ["underlying", "asset_type"]}


def _currency_grouped_schema(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """币种信封命令的参数 schema。

    所有这类命令都接受可选 ``account``；``allocation`` 与 ``attribution`` 另接
    ``groupBy``（受枚举约束）。

    @param extra: 额外的属性定义。
    @returns JSON Schema。
    """
    properties: dict[str, Any] = {"account": _ACCOUNT_PROPERTY}
    if extra:
        properties.update(extra)
    return {
        "type": "object",
        "properties": properties,
        "required": [],
        "additionalProperties": False,
    }


_GROUPED_SCHEMA = _currency_grouped_schema({"groupBy": _GROUP_BY_PROPERTY})

# 命令 → 参数 schema。``risk`` 需要必填 asOf，不在本批。
_GROUPED_SCHEMAS: dict[str, dict[str, Any]] = {
    command: (
        # risk 没有 asOf 就没有意义：缺省成"今天"会让结果随服务器日期漂移。
        {"type": "object",
         "properties": {"account": _ACCOUNT_PROPERTY,
                        "asOf": {"type": "string", "pattern": r"^\\d{4}-\\d{2}-\\d{2}$"}},
         "required": ["asOf"], "additionalProperties": False}
        if command == "risk"
        else _GROUPED_SCHEMA if command in {"allocation", "attribution"}
        else _currency_grouped_schema()
    )
    for command in CURRENCY_GROUPED_COMMANDS
}

# 详情类命令的参数 schema（对应 financial-tool-validation 里那套边界）。
_AS_OF = {"type": "string", "pattern": r"^\\d{4}-\\d{2}-\\d{2}$"}
_DETAIL_SCHEMAS: dict[str, dict[str, Any]] = {
    "positions": _currency_grouped_schema(
        {"underlying": {"type": "string", "minLength": 1}}),
    "expiring": {"type": "object",
                 "properties": {"account": _ACCOUNT_PROPERTY, "asOf": _AS_OF,
                                "days": {"type": "integer", "minimum": 1, "maximum": 365}},
                 "required": ["asOf"], "additionalProperties": False},
    "trades": _currency_grouped_schema(),
    "realized": _currency_grouped_schema(),
    "cashflow": _currency_grouped_schema(),
    "audit": _currency_grouped_schema(),
    "extremes": _currency_grouped_schema(
        {"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
}


def build_tool_definitions(
    connection_provider: Callable[[], duckdb.DuckDBPyConnection],
    market_runtime_provider: Callable[[], Any] | None = None,
) -> tuple[ToolDefinition, ...]:
    """装配当前已实现的工具。

    @param connection_provider: 返回当前连接的调用方；每次调用重新取，避免持有
        一个可能在重连后失效的连接。
    @param market_runtime_provider: 返回行情运行时的调用方；``None`` 表示未装配，
        此时持仓估值照常返回行、只是不含参考价（**数据状态而非错误**）。
    @returns 工具定义，顺序与 `financial_tools.FINANCIAL_TOOLS` 一致。
    """
    def scope_of(arguments: Mapping[str, Any]) -> AccountScope:
        """把可选 ``account`` 参数解析为账户范围。"""
        account = arguments.get("account")
        return AccountScope(kind="all") if not account else AccountScope(
            kind="account", account=account
        )

    def grouped(command: str) -> Callable[[Mapping[str, Any]], Any]:
        """生成一条币种信封命令的处理器。"""
        def handler(arguments: Mapping[str, Any]) -> Any:
            snapshot = DuckDbLedgerRepository(connection_provider()).load_snapshot(
                scope_of(arguments)
            )
            return handle_currency_grouped(command, snapshot, scope_of(arguments), arguments)
        return handler

    def accounts_handler(arguments: Mapping[str, Any]) -> Any:
        """账户列表。"""
        rows = connection_provider().execute(
            """
            SELECT id, display_name, broker, masked_suffix, base_currency, status, created_at
            FROM accounts
            """
        ).fetchall()
        accounts = [
            {
                "id": str(row[0]), "display_name": row[1], "broker": row[2],
                "masked_suffix": row[3], "base_currency": row[4], "status": row[5],
                "created_at": row[6].astimezone(__import__("datetime").timezone.utc)
                .isoformat(timespec="milliseconds").replace("+00:00", "Z")
                if hasattr(row[6], "astimezone") else str(row[6]),
            }
            for row in rows
        ]
        projected = project_account_list(accounts)
        return {"rows": projected, "count": len(projected)}

    def market_positions_handler(arguments: Mapping[str, Any]) -> Any:
        """持仓参考估值。

        **与路由共用同一份实现**（`handle_market_positions`）——各写一份会让字段与
        口径分叉，而 Agent 与 App 的用户都以为自己在看同一份数据。
        """
        runtime = market_runtime_provider() if market_runtime_provider is not None else None
        return handle_market_positions(
            connection_provider(), scope_of(arguments), runtime,
            arguments.get("underlying"),
        )

    definitions: list[ToolDefinition] = [
        ToolDefinition(
            name="accounts",
            description="列出账户（含已归档），不暴露券商完整标识",
            input_schema={"type": "object", "properties": {}, "required": [],
                          "additionalProperties": False},
            handler=accounts_handler,
        ),
        ToolDefinition(
            name="market-positions",
            description="当前未平持仓的参考估值（参考价不含费用；行情不可用时不含估值）",
            input_schema={
                "type": "object",
                "properties": {
                    "account": _ACCOUNT_PROPERTY,
                    "underlying": {"type": "string", "minLength": 1},
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=market_positions_handler,
        ),
    ]
    descriptions = {
        "summary": "账户总览：净投入、已实现损益、现金余额与费用",
        "monthly": "按月汇总已实现绩效",
        "allocation": "按标的历史成本配置",
        "attribution": "按标的的已实现损益归因",
        "drawdown": "日度回撤与恢复",
        "holding-period": "按持有期分桶的已实现绩效",
        "streaks": "最长连续盈利与亏损",
        "stress": "长期权权利金归零压力",
        "risk": "期权风险桶与标的集中度",
    }
    def detail(command: str) -> Callable[[Mapping[str, Any]], Any]:
        """生成一条详情命令的处理器。"""
        def handler(arguments: Mapping[str, Any]) -> Any:
            snapshot = DuckDbLedgerRepository(connection_provider()).load_snapshot(
                scope_of(arguments)
            )
            return handle_ledger_detail(command, snapshot, scope_of(arguments), arguments)
        return handler

    detail_descriptions = {
        "positions": "当前持仓（历史成本口径，非市值）",
        "expiring": "到期窗口内的未平期权",
        "trades": "交易流水",
        "realized": "已实现明细",
        "cashflow": "现金流水与分动作合计",
        "audit": "账本一致性审计（只读）",
        "extremes": "最佳与最差各 N 笔交易",
    }
    for command in LEDGER_DETAIL_COMMANDS:
        definitions.append(ToolDefinition(
            name=command,
            description=detail_descriptions[command],
            input_schema=_DETAIL_SCHEMAS[command],
            handler=detail(command),
        ))

    for command in CURRENCY_GROUPED_COMMANDS:
        definitions.append(ToolDefinition(
            name=command,
            description=descriptions[command],
            input_schema=_GROUPED_SCHEMAS[command],
            handler=grouped(command),
        ))
    return tuple(definitions)
