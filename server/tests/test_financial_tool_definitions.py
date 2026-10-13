"""工具定义装配测试。

最要紧的一条是 :func:`test_tool_result_matches_the_http_endpoint`：
**同一个分析经工具调用与经 HTTP 端点必须得到同样的形状**。若两侧各写一套分析，
字段与口径迟早分叉，而这种分叉在单侧测试里看不出来。

另一条是授权边界：**账户范围来自会话而不是模型参数**。会话绑定单账户时，一次不传
范围字段的调用必须仍然只看那个账户——否则"模型不传 account 就能查全部账户"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.financial_tool_definitions import build_tool_definitions
from lyra.application.financial_tool_handlers import CURRENCY_GROUPED_COMMANDS
from lyra.application.financial_tools import FINANCIAL_TOOL_NAMES, UNIMPLEMENTED_TOOLS
from lyra.application.tool_runtime import ToolRuntime
from lyra.db.connection import open_lyra_database
from lyra.domain.agent.tool_call import AgentToolContext
from lyra.domain.analytics.types import AccountScope

ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
AS_OF = "2026-09-01T00:00:00.000Z"


def context(*, account: str | None = None) -> AgentToolContext:
    """构造服务端注入的工具上下文。

    @param account: 绑定的账户 **UUID**；``None`` 表示全部账户范围。
    """
    return AgentToolContext(
        session_id="s-1", device_id="d-1", user_message_id="m-1",
        account_scope=(
            AccountScope(kind="all") if account is None
            else AccountScope(kind="account", account=account)
        ),
        ledger_version=3, as_of=AS_OF,
    )


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
        yield (
            ToolRuntime(build_tool_definitions(lambda: database.connection)),
            database.connection,
        )
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
    """``expiring`` 与 ``risk`` 的截止时间**由服务端注入**，模型无需也无法提供。

    这曾经是工具 schema 里的必填参数。改成注入之后，缺省成"今天"的风险消失，
    而模型也无法通过参数把窗口挪到自己想要的日期。
    """
    tool_runtime, _ = runtime
    for name in ("expiring", "risk"):
        assert tool_runtime.invoke(name, {}, context()) is not None


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

    via_tool = tool_runtime.invoke(command, {}, context())

    # 走一遍路由层的共享处理器，作为对照。
    from lyra.application.financial_tool_handlers import handle_currency_grouped
    from lyra.infrastructure.db.ledger_repository import DuckDbLedgerRepository

    snapshot = DuckDbLedgerRepository(connection).load_snapshot(AccountScope(kind="all"))
    via_handler = handle_currency_grouped(
        command, snapshot, AccountScope(kind="all"), {"asOf": "2026-09-01"}
    )

    assert via_tool == via_handler


def test_accounts_tool_returns_rows_and_count(runtime) -> None:
    """账户工具返回 rows/count 信封，字段为 camelCase。"""
    tool_runtime, _ = runtime
    result = tool_runtime.invoke("accounts", {}, context())

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
        with pytest.raises(ApplicationError) as raised:
            tool_runtime.invoke(name, arguments, context())
        assert raised.value.code == "TOOL_ARGUMENTS_INVALID", (name, arguments)


def test_reserved_scope_arguments_cannot_widen_the_session_scope(runtime) -> None:
    """**模型参数不得改写授权范围**：保留字段一律被拒，而不是被当成账户范围。

    会话绑定单账户时，模型传一个别的账户（或压根不传）都只能看到绑定账户的数据。
    """
    tool_runtime, _ = runtime

    for arguments in (
        {"account": "TST001"},
        {"account": OTHER_ACCOUNT_ID},
        {"account_scope": {"kind": "all"}},
        {"asOf": "2020-01-01"},
        {"ledger_version": 99},
    ):
        with pytest.raises(ApplicationError) as raised:
            tool_runtime.invoke("drawdown", arguments, context(account=ACCOUNT_ID))
        assert raised.value.code == "TOOL_ARGUMENTS_INVALID", arguments


def test_session_scope_is_applied_without_a_model_account_argument(runtime) -> None:
    """**会话绑定单账户时，模型不传范围也只能看到那个账户。**

    这是本次修复的核心边界：账户范围来自会话，不来自模型参数。
    """
    tool_runtime, _ = runtime

    bound = tool_runtime.invoke("drawdown", {}, context(account=ACCOUNT_ID))

    assert bound["aggregateCurrency"] == "USD"
    assert [group["accountIds"] for group in bound["groups"]] == [[ACCOUNT_ID]]
    # 未绑定的账户不是"空范围"，而是明确失败——静默返回空账本会被读成"没有交易"。
    with pytest.raises(ApplicationError) as raised:
        tool_runtime.invoke("drawdown", {}, context(account=OTHER_ACCOUNT_ID))
    assert raised.value.code == "ACCOUNT_NOT_FOUND"


def test_account_uuid_is_resolved_to_the_ledger_account_key(runtime) -> None:
    """**账户 UUID 必须被解析成账本脱敏键**，否则查询静默返回空账本。"""
    from lyra.infrastructure.db.account_lookup import resolve_legacy_account_key

    tool_runtime, connection = runtime
    assert resolve_legacy_account_key(connection, ACCOUNT_ID) == "TST001"

    result = tool_runtime.invoke("summary", {}, context(account=ACCOUNT_ID))

    assert result["aggregateCurrency"] == "USD"


def test_accounts_tool_hides_accounts_outside_the_session_scope(runtime) -> None:
    """会话绑定单账户时，``accounts`` 不得列出授权范围外的账户。"""
    tool_runtime, connection = runtime
    connection.execute(
        """
        INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), 'Other', 'TST002', '002')
        """,
        [OTHER_ACCOUNT_ID],
    )

    bound = tool_runtime.invoke("accounts", {}, context(account=ACCOUNT_ID))
    everything = tool_runtime.invoke("accounts", {}, context())

    assert [row["id"] for row in bound["rows"]] == [ACCOUNT_ID]
    assert {row["id"] for row in everything["rows"]} == {ACCOUNT_ID, OTHER_ACCOUNT_ID}


def test_public_descriptions_cover_all_bound_tools(runtime) -> None:
    """公开说明必须覆盖全部已绑定工具，否则模型发现不了它们。"""
    tool_runtime, _ = runtime
    described = {item["name"] for item in tool_runtime.public_descriptions()}
    assert described == set(tool_runtime.tool_names)


def test_model_facing_schemas_hide_server_injected_scope(runtime) -> None:
    """**模型可控 schema 里不得出现账户范围、账本版本或截止时间。**"""
    tool_runtime, _ = runtime

    for item in tool_runtime.public_descriptions():
        properties = item["inputSchema"].get("properties", {})
        for reserved in ("account", "accountScope", "account_scope",
                         "ledgerVersion", "ledger_version", "asOf", "as_of"):
            assert reserved not in properties, (item["name"], reserved)
