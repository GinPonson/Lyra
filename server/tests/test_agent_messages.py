"""Agent 消息与工具调用生命周期测试。

数据库对这两张表有状态—字段一致性约束。本模块把同一套规则实现在领域层，
**在写入前判定**——否则只会得到一个 `ConstraintException`，它只有列名，
没有"哪条规则被违反"。

最容易被忽略的是 **`completed` 不允许带错误码**：一个"完成但报错"的消息在语义上
自相矛盾，客户端无从判断该显示成功还是失败。
"""

from __future__ import annotations

import pytest

from lyra.domain.agent.messages import (
    finish_agent_message,
    finish_tool_call,
    reject_tool_call,
    start_agent_message,
    start_tool_call,
)


def _message():
    return start_agent_message(
        message_id="m-1", session_id="s-1", ordinal=1, role="assistant",
        content="", created_at="2026-10-12T09:00:00.000Z",
    )


def _call():
    return start_tool_call(
        call_id="c-1", session_id="s-1", message_id="m-1", ordinal=1,
        tool_name="summary",
        arguments_summary={"account": "TST001"}, ledger_version=3,
        started_at="2026-10-12T09:00:00.000Z",
    )


# --------------------------------------------------------------------------
# 消息
# --------------------------------------------------------------------------


def test_start_message_is_streaming() -> None:
    """新消息处于 streaming，无完成时间与错误码。"""
    message = _message()

    assert message.status == "streaming"
    assert message.completed_at is None
    assert message.error_code is None
    assert message.provider == "genkit"


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_finish_message_sets_completion_time(status: str) -> None:
    """三种终态都必须带完成时间。"""
    message = finish_agent_message(
        _message(), status=status, content="答案",  # type: ignore[arg-type]
        completed_at="2026-10-12T09:00:05.000Z",
    )

    assert message.status == status
    assert message.completed_at == "2026-10-12T09:00:05.000Z"
    assert message.content == "答案"


def test_completed_message_must_not_carry_an_error_code() -> None:
    """**"完成但报错"自相矛盾**，必须拒绝。

    若不拒绝，客户端拿到 status=completed 与 error_code 并存时无从判断该显示
    成功还是失败。
    """
    with pytest.raises(ValueError, match="completed 消息不能带错误码"):
        finish_agent_message(
            _message(), status="completed", error_code="TOOL_EXECUTION_FAILED",
            completed_at="2026-10-12T09:00:05.000Z",
        )


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_and_cancelled_may_carry_an_error_code(status: str) -> None:
    """失败与取消可以带错误码——那是客户端稳定分支的依据。"""
    message = finish_agent_message(
        _message(), status=status, error_code="TOOL_EXECUTION_FAILED",
        completed_at="2026-10-12T09:00:05.000Z",  # type: ignore[arg-type]
    )
    assert message.error_code == "TOOL_EXECUTION_FAILED"


def test_cannot_finish_a_message_twice() -> None:
    """已终止的消息不能再次终止——否则完成时间会被改写。"""
    finished = finish_agent_message(
        _message(), status="completed", completed_at="t1"
    )

    with pytest.raises(ValueError, match="已处于终态"):
        finish_agent_message(finished, status="failed", completed_at="t2")


def test_cannot_finish_with_streaming_status() -> None:
    """``streaming`` 不是终态，不能用它"终止"消息。"""
    with pytest.raises(ValueError, match="不能用 streaming 状态"):
        finish_agent_message(_message(), status="streaming", completed_at="t")


def test_ordinal_must_be_positive() -> None:
    """序号必须为正，与 schema 的 CHECK 一致。"""
    for ordinal in (0, -1):
        with pytest.raises(ValueError, match="正整数"):
            start_agent_message(
                message_id="m", session_id="s", ordinal=ordinal, role="user",
                content="x", created_at="t",
            )


def test_blank_identifiers_are_rejected() -> None:
    """空白标识符必须拒绝。"""
    with pytest.raises(ValueError, match="不能为空"):
        start_agent_message(
            message_id="   ", session_id="s", ordinal=1, role="user",
            content="x", created_at="t",
        )


# --------------------------------------------------------------------------
# 工具调用
# --------------------------------------------------------------------------


def test_start_tool_call_records_the_authorization_decision() -> None:
    """授权判定必须随调用一起记录——拒绝的调用不会执行，但痕迹要留。"""
    call = _call()

    assert call.status == "started"
    assert call.authorization_decision == "allowed"
    assert call.ledger_version == 3
    assert call.result_summary is None


def test_denied_call_is_recorded_as_rejected_not_started() -> None:
    """**被拒的调用从未开始**：状态直接是 ``rejected``，没有耗时。

    数据库的 CHECK 也这样要求（``denied`` 必须配 ``rejected``）。仍要落库是因为
    它是审计的一部分——事后需要能回答"模型是否尝试过访问授权范围外的账户"。
    """
    call = reject_tool_call(
        call_id="c-1", session_id="s", message_id="m", ordinal=1,
        tool_name="trades", arguments_summary={"account": "OTHER"},
        ledger_version=0, rejected_at="t",
    )

    assert call.authorization_decision == "denied"
    assert call.status == "rejected"
    assert call.duration_ms is None
    assert call.completed_at == "t"


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "rejected"])
def test_finish_tool_call(status: str) -> None:
    """四种终态都可终止。"""
    finished = finish_tool_call(
        _call(), status=status, duration_ms=12,  # type: ignore[arg-type]
        completed_at="t2",
    )

    assert finished.status == status
    assert finished.duration_ms == 12


def test_cannot_finish_a_tool_call_twice() -> None:
    """已终止的调用不能再次终止。"""
    finished = finish_tool_call(_call(), status="completed", completed_at="t2")

    with pytest.raises(ValueError, match="已处于终态"):
        finish_tool_call(finished, status="failed", completed_at="t3")


def test_cannot_finish_with_started_status() -> None:
    """``started`` 不是终态。"""
    with pytest.raises(ValueError, match="不能用 started 状态"):
        finish_tool_call(_call(), status="started", completed_at="t")


def test_negative_ledger_version_is_rejected() -> None:
    """账本版本非负。"""
    with pytest.raises(ValueError, match="非负整数"):
        start_tool_call(
            call_id="c", session_id="s", message_id="m", ordinal=1,
            tool_name="t", arguments_summary={}, ledger_version=-1, started_at="t",
        )


def test_arguments_summary_is_copied() -> None:
    """摘要按值复制，避免调用方后续改动影响已构造的记录。"""
    arguments: dict[str, object] = {"account": "TST001"}
    call = start_tool_call(
        call_id="c", session_id="s", message_id="m", ordinal=1, tool_name="t",
        arguments_summary=arguments, ledger_version=0, started_at="t",
    )

    arguments["account"] = "MUTATED"

    assert call.arguments_summary == {"account": "TST001"}
