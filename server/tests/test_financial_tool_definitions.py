"""工具定义装配测试。

最要紧的一条是 :func:`test_tool_result_matches_the_http_endpoint`：
**同一个分析经工具调用与经 HTTP 端点必须得到同样的形状**。若两侧各写一套分析，
字段与口径迟早分叉，而这种分叉在单侧测试里看不出来。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra.application.financial_tool_definitions import build_tool_definitions
from lyra.application.financial_tool_handlers import CURRENCY_GROUPED_COMMANDS
from lyra.application.financial_tools import FINANCIAL_TOOL_NAMES, UNIMPLEMENTED_TOOLS
from lyra.application.tool_runtime import ToolRuntime
from lyra.db.connection import open_lyra_database

ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def runtime(tmp_path: Path):
    """在临时真实库上装配工具运行时。"""
    database = open_lyra_database(str(tmp_path / "tools.duckdb"))
    database.connection.execute(
        """
        INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), 'Account', 'TST001', '001')
        """,
        [ACCOUNT_ID],
    )
    try:
        yield ToolRuntime(build_tool_definitions(lambda: database.connection)), database.connection
    finally:
        database.close()


def test_bound_tools_are_a_subset_of_the_catalog(runtime) -> None:
    """绑定的工具名必须都在目录里——拼错名字会让 Agent 调不到实现。"""
    tool_runtime, _ = runtime
    for name in tool_runtime.tool_names:
        assert name in FINANCIAL_TOOL_NAMES, f"工具 {name} 不在目录中"


def test_every_currency_grouped_command_is_bound(runtime) -> None:
    """已注册的币种信封命令（含 summary）必须全部有工具定义。"""
    tool_runtime, _ = runtime
    for command in CURRENCY_GROUPED_COMMANDS:
        assert command in tool_runtime.tool_names, f"{command} 未绑定工具"


def test_accounts_is_bound(runtime) -> None:
    """``accounts`` 已实现，必须绑定。"""
    tool_runtime, _ = runtime
    assert "accounts" in tool_runtime.tool_names


def test_bound_tools_cover_the_catalog_exactly(runtime) -> None:
    """**已绑定工具 = 目录去掉未实现项**，一个不多一个不少。

    缺一个 → Agent 调不到该能力；多一个 → 调了会 404。两种都是静默失败，只有对账能拦。
    """
    tool_runtime, _ = runtime
    expected = set(FINANCIAL_TOOL_NAMES) - UNIMPLEMENTED_TOOLS

    assert set(tool_runtime.tool_names) == expected
    assert len(expected) == 18


def test_every_detail_command_is_bound(runtime) -> None:
    """已注册的详情命令必须全部有工具定义。"""
    from lyra.application.financial_tool_handlers import LEDGER_DETAIL_COMMANDS

    tool_runtime, _ = runtime
    for command in LEDGER_DETAIL_COMMANDS:
        assert command in tool_runtime.tool_names, f"{command} 未绑定工具"


def test_detail_tools_require_as_of_where_needed(runtime) -> None:
    """``expiring`` 与 ``risk`` 缺 ``asOf`` 必须被拒。"""
    tool_runtime, _ = runtime
    for name in ("expiring", "risk"):
        with pytest.raises(Exception) as raised:
            tool_runtime.invoke(name, {})
        assert getattr(raised.value, "code", None) == "TOOL_ARGUMENTS_INVALID", name


def test_no_unimplemented_tool_is_bound(runtime) -> None:
    """未实现的工具**不得**出现在运行时里——否则 Agent 会调用到 404。"""
    tool_runtime, _ = runtime
    assert UNIMPLEMENTED_TOOLS.isdisjoint(set(tool_runtime.tool_names))


@pytest.mark.parametrize("command", ["summary", "drawdown", "stress"])
def test_tool_result_matches_the_http_endpoint(runtime, command: str) -> None:
    """**工具调用与 HTTP 端点必须产出同一形状。**

    两侧若各写一套分析，字段与口径会分叉，而单侧测试看不出来。
    这里直接比对两者的响应体。
    """
    tool_runtime, connection = runtime

    via_tool = tool_runtime.invoke(command, {})

    # 走一遍路由层的共享处理器，作为对照。
    from lyra.application.financial_tool_handlers import handle_currency_grouped
    from lyra.domain.analytics.types import AccountScope
    from lyra.infrastructure.db.ledger_repository import DuckDbLedgerRepository

    snapshot = DuckDbLedgerRepository(connection).load_snapshot(AccountScope(kind="all"))
    via_handler = handle_currency_grouped(command, snapshot, AccountScope(kind="all"), {})

    assert via_tool == via_handler


def test_accounts_tool_returns_rows_and_count(runtime) -> None:
    """账户工具返回 rows/count 信封，字段为 camelCase。"""
    tool_runtime, _ = runtime
    result = tool_runtime.invoke("accounts", {})

    assert result["count"] == 1
    row = result["rows"][0]
    assert row["id"] == ACCOUNT_ID
    assert row["maskedSuffix"] == "001"
    assert "legacyAccountKey" not in row


def test_invalid_arguments_are_rejected(runtime) -> None:
    """工具的参数边界生效：类型不符与枚举外取值都被拒。"""
    tool_runtime, _ = runtime

    for name, arguments in (
        ("drawdown", {"account": 5}),
        ("allocation", {"groupBy": "asset"}),
        ("drawdown", {"unknown": 1}),
    ):
        with pytest.raises(Exception) as raised:
            tool_runtime.invoke(name, arguments)
        assert getattr(raised.value, "code", None) == "TOOL_ARGUMENTS_INVALID", (name, arguments)


def test_account_scope_argument_filters_results(runtime) -> None:
    """``account`` 参数必须真正作用于分析范围。"""
    tool_runtime, _ = runtime

    included = tool_runtime.invoke("drawdown", {"account": "TST001"})
    excluded = tool_runtime.invoke("drawdown", {"account": "NOPE"})

    assert included["aggregateCurrency"] == "USD"
    assert excluded["groups"] == []


def test_public_descriptions_cover_all_bound_tools(runtime) -> None:
    """公开说明必须覆盖全部已绑定工具，否则模型发现不了它们。"""
    tool_runtime, _ = runtime
    described = {item["name"] for item in tool_runtime.public_descriptions()}
    assert described == set(tool_runtime.tool_names)
