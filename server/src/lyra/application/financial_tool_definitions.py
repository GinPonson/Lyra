"""财务工具的 `ToolDefinition` 装配。

把工具名、参数 schema 与处理器绑成 :class:`ToolDefinition`，供 Agent 与 REST 复用。
处理器一律调用 :mod:`financial_tool_handlers` 的共享实现——**不在这里重写分析逻辑**，
否则路由与 Agent 会各有一份，字段与口径必然分叉。

**账户范围、账本版本与截止时间由服务端会话注入，不进入模型可控 schema**：会话绑定
单账户时，一个不传范围字段的调用会静默升级为"全部账户"，而系统提示词里的授权说明
拦不住它——提示词不是访问控制。

本模块绑定全部 **18 个工具**（7 条币种信封命令 + 7 条详情命令 + `accounts` +
`market-positions`）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import duckdb

from ..domain.agent.tool_call import AgentToolContext
from ..domain.analytics.types import AccountScope
from ..infrastructure.db.account_lookup import resolve_legacy_account_key
from ..infrastructure.db.ledger_repository import DuckDbLedgerRepository
from .app_error import ApplicationError
from .financial_tool_handlers import (
    CURRENCY_GROUPED_COMMANDS,
    LEDGER_DETAIL_COMMANDS,
    handle_currency_grouped,
    handle_ledger_detail,
    handle_market_positions,
)
from .public_projection import project_account_list
from .tool_runtime import ToolDefinition

_GROUP_BY_PROPERTY = {"type": "string", "enum": ["underlying", "asset_type"]}

# 模型可控参数里**永远不得生效**的保留字段。schema 已经不含它们，这里再删一次：
# 授权边界不能只靠一层 schema——任何绕过校验的入口都不该通过参数改写账户范围、
# 账本版本或截止时间。
_RESERVED_ARGUMENTS = frozenset({
    "account", "account_scope", "accountScope",
    "ledger_version", "ledgerVersion", "as_of", "asOf",
})


def _command_schema(properties: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造只含模型可控字段的参数 schema。

    @param properties: 该命令独有的模型可控属性。
    @returns JSON Schema。
    """
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": [],
        "additionalProperties": False,
    }


_GROUPED_SCHEMA = _command_schema({"groupBy": _GROUP_BY_PROPERTY})

# 命令 → 参数 schema。``risk`` 需要的 asOf 由服务端注入（见 `AgentToolContext`），
# 因此它的 schema 是空的：缺省成"今天"会让结果随服务器日期漂移。
_GROUPED_SCHEMAS: dict[str, dict[str, Any]] = {
    command: (
        _GROUPED_SCHEMA if command in {"allocation", "attribution"} else _command_schema()
    )
    for command in CURRENCY_GROUPED_COMMANDS
}

# 详情类命令的参数 schema（对应 financial-tool-validation 里那套边界）。
_DETAIL_SCHEMAS: dict[str, dict[str, Any]] = {
    "positions": _command_schema({"underlying": {"type": "string", "minLength": 1}}),
    "expiring": _command_schema(
        {"days": {"type": "integer", "minimum": 1, "maximum": 365}}),
    "trades": _command_schema(),
    "realized": _command_schema(),
    "cashflow": _command_schema(),
    "audit": _command_schema(),
    "extremes": _command_schema(
        {"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
}


def _authorized_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """删掉保留字段后的参数副本。"""
    return {key: value for key, value in arguments.items() if key not in _RESERVED_ARGUMENTS}


def _as_utc_date(as_of: str) -> str:
    """把会话截止时间戳收敛为 UTC 日历日。

    到期窗口（``expiring`` / ``risk``）按**日历日**比较；拿时间戳去比较会得到
    ``2026-09-01T00:00:00.000Z`` 这种字符串，静默算出错误的窗口。

    @param as_of: 会话的 ISO 截止时间。
    @returns ``YYYY-MM-DD``。
    @raises ApplicationError: 时间戳无法解析。
    """
    try:
        parsed = datetime.fromisoformat(as_of.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ApplicationError(
            code="AGENT_AS_OF_INVALID",
            message="Agent 会话数据截止时间无效",
            status_code=500,
            retryable=False,
        ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).date().isoformat()


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
    def resolved_scope(context: AgentToolContext) -> AccountScope:
        """把会话授权的账户范围解析为账本用的账户键。

        **会话里的账户是 UUID，而账本查询要的是脱敏账户键**；两者都是字符串，直接
        透传不会报错，只会匹配不到任何行并返回空账本——而空账本会被读成"这个账户
        没有交易"。查不到时显式报错，绝不退化成空范围或全范围。

        @param context: 服务端注入的工具上下文。
        @returns 账本可用的账户范围。
        @raises ApplicationError: 会话未绑定账户，或绑定的账户不存在。
        """
        scope = context.account_scope
        if scope.kind == "all":
            return AccountScope(kind="all")
        if not scope.account:
            raise ApplicationError(
                code="ACCOUNT_SCOPE_INVALID",
                message="Agent 会话未绑定账户范围",
                status_code=400,
                retryable=False,
            )
        key = resolve_legacy_account_key(connection_provider(), scope.account)
        if key is None:
            raise ApplicationError(
                code="ACCOUNT_NOT_FOUND",
                message="Agent 会话绑定的账户不存在",
                status_code=404,
                retryable=False,
            )
        return AccountScope(kind="account", account=key)

    def effective_arguments(
        context: AgentToolContext, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        """把模型参数与服务端注入的口径合成处理器输入。

        ``asOf`` 由服务端注入：它不在模型可控 schema 里，缺省成"今天"会让同一请求的
        结果随服务器日期漂移（``risk`` / ``expiring`` 的必填语义）。
        """
        return {**_authorized_arguments(arguments), "asOf": _as_utc_date(context.as_of)}

    def grouped(command: str) -> Callable[[Mapping[str, Any], AgentToolContext], Any]:
        """生成一条币种信封命令的处理器。"""
        def handler(arguments: Mapping[str, Any], context: AgentToolContext) -> Any:
            scope = resolved_scope(context)
            snapshot = DuckDbLedgerRepository(connection_provider()).load_snapshot(scope)
            return handle_currency_grouped(
                command, snapshot, scope, effective_arguments(context, arguments)
            )
        return handler

    def accounts_handler(arguments: Mapping[str, Any], context: AgentToolContext) -> Any:
        """账户列表；**只返回会话授权范围内的账户**。

        会话绑定单账户时若仍列出全部账户，模型可以直接读出授权范围外的账户，
        再针对它们发问——授权边界就只剩提示词里的一句话。
        """
        scope = context.account_scope
        if scope.kind == "account":
            # 先解析一次：账户不存在时显式失败，而不是返回空列表被读成"没有账户"。
            resolved_scope(context)
            rows = connection_provider().execute(
                """
                SELECT id, display_name, broker, masked_suffix, base_currency, status,
                       created_at
                FROM accounts
                WHERE id = CAST(? AS UUID)
                """,
                [scope.account],
            ).fetchall()
        else:
            rows = connection_provider().execute(
                """
                SELECT id, display_name, broker, masked_suffix, base_currency, status,
                       created_at
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

    def market_positions_handler(
        arguments: Mapping[str, Any], context: AgentToolContext
    ) -> Any:
        """持仓参考估值。

        **与路由共用同一份实现**（`handle_market_positions`）——各写一份会让字段与
        口径分叉，而 Agent 与 App 的用户都以为自己在看同一份数据。
        """
        runtime = market_runtime_provider() if market_runtime_provider is not None else None
        return handle_market_positions(
            connection_provider(), resolved_scope(context), runtime,
            _authorized_arguments(arguments).get("underlying"),
        )

    definitions: list[ToolDefinition] = [
        ToolDefinition(
            name="accounts",
            description="列出会话授权范围内的账户，含已归档账户，不暴露券商完整标识",
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
    def detail(command: str) -> Callable[[Mapping[str, Any], AgentToolContext], Any]:
        """生成一条详情命令的处理器。"""
        def handler(arguments: Mapping[str, Any], context: AgentToolContext) -> Any:
            scope = resolved_scope(context)
            snapshot = DuckDbLedgerRepository(connection_provider()).load_snapshot(scope)
            return handle_ledger_detail(
                command, snapshot, scope, effective_arguments(context, arguments)
            )
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
