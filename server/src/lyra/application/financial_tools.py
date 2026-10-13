"""财务工具目录。

Agent 通过工具名调用账本分析。工具名、顺序与 HTTP 路径必须与 TypeScript 侧一致：
Flutter 与 Agent 的提示词都按名字引用工具，改名或换序会同时打断两边。

本模块是**单一事实来源**：路由层按它挂载端点，Agent 层按它枚举可调用工具。
因此有一条测试把目录与**实际已挂载的路由**对账，防止两者漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ToolCategory = Literal["ledger", "analytics", "market"]


@dataclass(frozen=True)
class FinancialToolSpec:
    """一个财务工具的固定描述。

    @param name: 工具名，也是 Agent 调用时使用的标识。
    @param category: 分组，决定路由前缀与提示词归类。
    @param path: 对应的 HTTP 路径；目录因此可直接用于对照。
    """

    name: str
    category: ToolCategory
    path: str


# 工具目录：顺序与 TypeScript 侧一致。
FINANCIAL_TOOLS: tuple[FinancialToolSpec, ...] = (
    FinancialToolSpec("accounts", "ledger", "/v1/ledger/accounts"),
    FinancialToolSpec("summary", "ledger", "/v1/ledger/summary"),
    FinancialToolSpec("positions", "ledger", "/v1/ledger/positions"),
    FinancialToolSpec("expiring", "ledger", "/v1/ledger/expiring"),
    FinancialToolSpec("trades", "ledger", "/v1/ledger/trades"),
    FinancialToolSpec("realized", "ledger", "/v1/ledger/realized"),
    FinancialToolSpec("cashflow", "ledger", "/v1/ledger/cashflow"),
    FinancialToolSpec("audit", "ledger", "/v1/ledger/audit"),
    FinancialToolSpec("monthly", "analytics", "/v1/analytics/monthly"),
    FinancialToolSpec("allocation", "analytics", "/v1/analytics/allocation"),
    FinancialToolSpec("attribution", "analytics", "/v1/analytics/attribution"),
    FinancialToolSpec("drawdown", "analytics", "/v1/analytics/drawdown"),
    FinancialToolSpec("risk", "analytics", "/v1/analytics/risk"),
    FinancialToolSpec("holding-period", "analytics", "/v1/analytics/holding-period"),
    FinancialToolSpec("extremes", "analytics", "/v1/analytics/extremes"),
    FinancialToolSpec("streaks", "analytics", "/v1/analytics/streaks"),
    FinancialToolSpec("stress", "analytics", "/v1/analytics/stress"),
    FinancialToolSpec("market-positions", "market", "/v1/market/positions"),
)

FINANCIAL_TOOL_NAMES: tuple[str, ...] = tuple(tool.name for tool in FINANCIAL_TOOLS)

_BY_NAME = {tool.name: tool for tool in FINANCIAL_TOOLS}

# 尚未在 Python 侧实现的工具。**显式列出而不是靠推断**：这样实现补上后必须同时
# 改这里，目录与实现不会悄悄分叉。
UNIMPLEMENTED_TOOLS: frozenset[str] = frozenset()


def is_financial_tool_name(value: str) -> bool:
    """判断是否为已知工具名。"""
    return value in _BY_NAME


def tool_spec(name: str) -> FinancialToolSpec:
    """按名字取出工具描述。

    @param name: 工具名。
    @returns 工具描述。
    @raises KeyError: 未知工具名。
    """
    return _BY_NAME[name]


def tools_in_category(category: ToolCategory) -> tuple[FinancialToolSpec, ...]:
    """取出某个分组的全部工具，保持目录顺序。"""
    return tuple(tool for tool in FINANCIAL_TOOLS if tool.category == category)


def tools_by_path() -> dict[str, FinancialToolSpec]:
    """路径到工具描述的映射，供路由层对账。

    @raises ValueError: 两个工具映射到同一路径——那会让对账失去意义。
    """
    mapping: dict[str, FinancialToolSpec] = {}
    for tool in FINANCIAL_TOOLS:
        if tool.path in mapping:
            raise ValueError(f"工具路径重复: {tool.path}")
        mapping[tool.path] = tool
    return mapping
