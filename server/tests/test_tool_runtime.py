"""工具分发器测试。

三类失败必须分开，否则模型会收到误导性的反馈：

- 工具名不存在 → 提示词需要修正，不是重试参数；
- 参数非法 → 可带着校验信息重试；
- 处理器**意外**异常 → 必须折叠，**不得把内部异常文本透给模型**。

最后一条是安全性质：内部异常常含 SQL、路径或连接串，把它们当作工具结果喂回模型，
等于把服务端内部细节写进对话记录。
"""

from __future__ import annotations

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.tool_runtime import ToolDefinition, ToolRuntime
from lyra.domain.agent.tool_call import AgentToolContext
from lyra.domain.analytics.types import AccountScope

SCHEMA: dict = {
    "type": "object",
    "properties": {"asOf": {"type": "string"}},
    "required": ["asOf"],
    "additionalProperties": False,
}


def _context() -> AgentToolContext:
    """一个会话范围内的服务端上下文。"""
    return AgentToolContext(
        session_id="s-1", device_id="d-1", user_id="u-1", user_message_id="m-1",
        account_scope=AccountScope(kind="all"), ledger_version=3,
        as_of="2026-09-01T00:00:00.000Z",
    )


def _runtime(handler=None) -> ToolRuntime:
    """构造只带一个工具的分发器。"""
    return ToolRuntime((ToolDefinition(
        name="risk",
        description="期权风险桶",
        input_schema=SCHEMA,
        handler=handler or (lambda arguments, context: {"asOf": arguments["asOf"]}),
    ),))


def test_invokes_a_valid_tool() -> None:
    """合法调用返回处理器结果。"""
    assert _runtime().invoke("risk", {"asOf": "2026-09-01"}, _context()) == {"asOf": "2026-09-01"}


def test_unknown_tool_is_a_distinct_error() -> None:
    """未知工具与参数非法必须是不同错误码。"""
    with pytest.raises(ApplicationError) as raised:
        _runtime().invoke("nonexistent", {}, _context())

    assert raised.value.code == "TOOL_NOT_FOUND"
    assert raised.value.status_code == 400


def test_invalid_arguments_are_rejected_before_the_handler_runs() -> None:
    """参数非法时**不得执行处理器**。"""
    called: list[object] = []
    runtime = _runtime(lambda arguments, context: called.append(arguments))

    with pytest.raises(ApplicationError) as raised:
        runtime.invoke("risk", {"asOf": 5}, _context())

    assert raised.value.code == "TOOL_ARGUMENTS_INVALID"
    assert called == [], "参数非法却执行了处理器"


def test_missing_required_argument_is_rejected() -> None:
    """缺少必填项同样在处理器前被拦下。"""
    with pytest.raises(ApplicationError) as raised:
        _runtime().invoke("risk", {}, _context())
    assert raised.value.code == "TOOL_ARGUMENTS_INVALID"


def test_expected_errors_pass_through() -> None:
    """处理器抛出的 ``ApplicationError`` 必须透传，不能被折叠。"""

    def handler(arguments, context):
        raise ApplicationError(
            code="ACCOUNT_FORBIDDEN", message="无权访问该账户", status_code=403, retryable=False
        )

    with pytest.raises(ApplicationError) as raised:
        _runtime(handler).invoke("risk", {"asOf": "2026-09-01"}, _context())

    assert raised.value.code == "ACCOUNT_FORBIDDEN"
    assert raised.value.status_code == 403


def test_unexpected_errors_are_folded_without_leaking_internals() -> None:
    """**内部异常文本不得出现在对外错误里。**"""
    secret = "psycopg2 error: connection string postgres://user:pw@host:5432/lyra"

    def handler(arguments, context):
        raise RuntimeError(secret)

    with pytest.raises(ApplicationError) as raised:
        _runtime(handler).invoke("risk", {"asOf": "2026-09-01"}, _context())

    assert raised.value.code == "TOOL_EXECUTION_FAILED"
    assert raised.value.status_code == 500
    assert "postgres://" not in raised.value.message
    assert "psycopg2" not in raised.value.message
    assert secret not in str(raised.value.details)


def test_duplicate_tool_names_are_rejected() -> None:
    """工具名重复必须报错，而不是后者覆盖前者。"""
    definition = ToolDefinition("risk", "d", SCHEMA, lambda arguments, context: None)
    with pytest.raises(ValueError, match="工具名重复"):
        ToolRuntime((definition, definition))


def test_tool_names_preserve_registration_order() -> None:
    """工具名顺序稳定，便于提示词与客户端按固定顺序展示。"""
    runtime = ToolRuntime(tuple(
        ToolDefinition(name, "d", {}, lambda arguments, context: None)
        for name in ("accounts", "summary", "risk")
    ))
    assert runtime.tool_names == ("accounts", "summary", "risk")


def test_public_descriptions_expose_schema() -> None:
    """公开说明包含参数 schema，使模型与客户端能发现参数边界。"""
    descriptions = _runtime().public_descriptions()

    assert len(descriptions) == 1
    assert descriptions[0]["name"] == "risk"
    assert descriptions[0]["inputSchema"] == SCHEMA


def test_definition_lookup_raises_for_unknown() -> None:
    """直接取定义时未知工具也报 ``TOOL_NOT_FOUND``。"""
    with pytest.raises(ApplicationError) as raised:
        _runtime().definition("nope")
    assert raised.value.code == "TOOL_NOT_FOUND"


def test_handler_receives_original_arguments_and_the_server_context() -> None:
    """处理器拿到**原样**参数与服务端上下文——校验只回答"能不能调用"。"""
    seen: list = []
    runtime = _runtime(lambda arguments, context: seen.append((arguments, context)))

    runtime.invoke("risk", {"asOf": "2026-09-01"}, _context())

    assert seen == [({"asOf": "2026-09-01"}, _context())]
