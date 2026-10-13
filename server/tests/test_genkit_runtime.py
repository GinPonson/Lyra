"""Genkit 执行器信号映射测试。

最要紧的一条是 :func:`test_reasoning_never_emits_the_original_text`：
供应商的推理活动只映射为 `ReasoningDelta("active")`，**原文不进入任何下游**。

另一条同样重要：**无论成功失败都必须发 `TurnSettled()`**。少了它，编排器会认为
回合还没结束，会话永远停在 running。
"""

from __future__ import annotations

import pytest

from lyra.domain.agent.turn_stream import (
    MessageDelta,
    MessageFailed,
    MessageSnapshot,
    ReasoningDelta,
    TurnSettled,
    UsageUpdated,
)
from lyra.infrastructure.agents.genkit_runtime import (
    ModelChunk,
    ModelToolRequest,
    ModelUsage,
    run_agent_turn_stream,
)


class FakeStream:
    """按脚本产生分片的假流。"""

    def __init__(
        self, *, chunks=(), tools=(), usage=None, final="", error=None
    ) -> None:
        self._chunks = list(chunks)
        self._tools = tuple(tools)
        self._usage = usage
        self._final = final
        self._error = error

    def chunks(self):
        if self._error is not None:
            raise self._error
        return list(self._chunks)

    def tool_requests(self):
        return self._tools

    def usage(self):
        return self._usage

    def final_text(self):
        return self._final


def _run(stream, *, tools=(), execute=None, captured=None):
    """执行一次回合并返回发出的信号。"""
    signals: list = []
    calls: list = []
    run_agent_turn_stream(
        content="这个月赚了多少",
        system_context="系统提示",
        tool_definitions=tuple(tools),
        stream_provider=lambda **kwargs: stream,
        execute_tool=execute or (lambda name, args: calls.append((name, args))),
        emit=signals.append,
    )
    if captured is not None:
        captured.extend(calls)
    return signals


# --------------------------------------------------------------------------
# 映射
# --------------------------------------------------------------------------


def test_text_chunks_map_to_message_deltas() -> None:
    """可见文本逐块映射为 message_delta。"""
    signals = _run(FakeStream(
        chunks=[ModelChunk(text="本月"), ModelChunk(text="净收益 200")],
        final="本月净收益 200",
    ))

    deltas = [signal for signal in signals if isinstance(signal, MessageDelta)]
    assert [delta.delta for delta in deltas] == ["本月", "净收益 200"]


def test_reasoning_never_emits_the_original_text() -> None:
    """**推理活动只映射为 `"active"`，原文不进入任何下游。**"""
    signals = _run(FakeStream(
        chunks=[ModelChunk(reasoning=True), ModelChunk(text="答案")], final="答案"
    ))

    reasoning = [signal for signal in signals if isinstance(signal, ReasoningDelta)]
    assert reasoning == [ReasoningDelta(delta="active")]
    # 原文从未出现在任何信号里。
    assert "active" in str(reasoning[0])


def test_usage_maps_with_null_cost() -> None:
    """用量映射；**成本不猜**，未提供时为空。"""
    signals = _run(FakeStream(
        chunks=[ModelChunk(text="x")], usage=ModelUsage(total_tokens=120), final="x"
    ))

    usage = [signal for signal in signals if isinstance(signal, UsageUpdated)]
    assert usage == [UsageUpdated(total_tokens=120, total_cost=None)]


def test_no_usage_emits_no_signal() -> None:
    """供应商未上报用量时不发用量事件。"""
    signals = _run(FakeStream(chunks=[ModelChunk(text="x")], final="x"))
    assert not any(isinstance(signal, UsageUpdated) for signal in signals)


def test_final_text_becomes_the_authoritative_snapshot() -> None:
    """结束时发权威快照，覆盖增量聚合。"""
    signals = _run(FakeStream(
        chunks=[ModelChunk(text="本月"), ModelChunk(text="净收益 200")],
        final="本月净收益 200",
    ))

    snapshots = [signal for signal in signals if isinstance(signal, MessageSnapshot)]
    assert len(snapshots) == 1
    assert snapshots[0].content == "本月净收益 200"
    assert snapshots[0].stop_reason == "end_turn"


def test_signal_order_is_deltas_then_snapshot_then_settled() -> None:
    """信号顺序：增量 → 快照 → 收口。"""
    signals = _run(FakeStream(chunks=[ModelChunk(text="x")], final="x"))
    kinds = [type(signal).__name__ for signal in signals]

    assert kinds == ["MessageDelta", "MessageSnapshot", "TurnSettled"]


# --------------------------------------------------------------------------
# 失败与收口
# --------------------------------------------------------------------------


def test_provider_error_maps_to_failure_and_still_settles() -> None:
    """供应商出错 → 失败终态，**并且仍然收口**。"""
    signals = _run(FakeStream(error=RuntimeError("upstream 500: internal detail")))

    assert any(isinstance(signal, MessageFailed) for signal in signals)
    assert isinstance(signals[-1], TurnSettled)


def test_failure_does_not_leak_upstream_detail() -> None:
    """失败信号里只有稳定终态，不含上游错误文本。"""
    signals = _run(FakeStream(error=RuntimeError("postgres://user:pw@host")))

    assert "postgres://" not in str(signals)


def test_success_still_settles() -> None:
    """成功也必须收口。"""
    signals = _run(FakeStream(chunks=[ModelChunk(text="x")], final="x"))
    assert isinstance(signals[-1], TurnSettled)


def test_always_settles_exactly_once() -> None:
    """收口信号恰好一条。"""
    for stream in (
        FakeStream(chunks=[ModelChunk(text="x")], final="x"),
        FakeStream(error=RuntimeError("boom")),
    ):
        signals = _run(stream)
        assert sum(isinstance(s, TurnSettled) for s in signals) == 1


# --------------------------------------------------------------------------
# 工具调用
# --------------------------------------------------------------------------


class FakeDefinition:
    """最小工具定义。"""

    def __init__(self, name: str, schema: dict) -> None:
        self.name = name
        self.input_schema = schema


def test_tool_requests_are_forwarded() -> None:
    """模型请求的工具调用被转发给执行器。"""
    definition = FakeDefinition("summary", {"type": "object", "properties": {}})
    calls: list = []

    _run(
        FakeStream(
            chunks=[ModelChunk(text="x")], final="x",
            tools=[ModelToolRequest(name="summary", arguments={"account": "TST001"})],
        ),
        tools=[definition],
        captured=calls,
    )

    assert calls == [("summary", {"account": "TST001"})]


def test_invalid_arguments_are_still_forwarded_for_audit() -> None:
    """**参数非法也要转发**——否则事后无法回答"模型是否尝试过错误调用"。"""
    definition = FakeDefinition(
        "summary", {"type": "object", "properties": {"n": {"type": "integer"}}}
    )
    calls: list = []

    _run(
        FakeStream(
            chunks=[], final="",
            tools=[ModelToolRequest(name="summary", arguments={"n": "not-an-integer"})],
        ),
        tools=[definition],
        captured=calls,
    )

    assert len(calls) == 1
    assert calls[0][0] == "summary"


def test_unknown_tool_is_still_forwarded_for_audit() -> None:
    """未知工具同样转发，由执行器记录拒绝。"""
    calls: list = []

    _run(
        FakeStream(chunks=[], final="", tools=[ModelToolRequest(name="nope", arguments={})]),
        tools=[],
        captured=calls,
    )

    assert calls == [("nope", {})]


def test_sensitive_arguments_are_stripped_from_the_summary() -> None:
    """**敏感字段不得进入审计摘要。**"""
    definition = FakeDefinition("summary", {"type": "object", "properties": {}})
    calls: list = []

    _run(
        FakeStream(
            chunks=[], final="",
            tools=[ModelToolRequest(
                name="summary", arguments={"accessToken": "sk-secret", "account": "TST001"}
            )],
        ),
        tools=[definition],
        captured=calls,
    )

    assert calls[0][1] == {}, "净化失败时应退回空摘要，而不是带上敏感值"
    assert "sk-secret" not in str(calls)


def test_unsafe_arguments_fall_back_to_empty_summary() -> None:
    """净化失败退回空摘要：审计记录不该因一个不可摘要的参数而整体丢失。"""
    definition = FakeDefinition("summary", {"type": "object", "properties": {}})
    calls: list = []

    _run(
        FakeStream(
            chunks=[], final="",
            tools=[ModelToolRequest(name="summary", arguments={"payload": {"nested": 1}})],
        ),
        tools=[definition],
        captured=calls,
    )

    assert calls[0][1] == {}


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------


def test_factory_returns_none_when_unconfigured() -> None:
    """模型未配置时返回 None——路由据此返回 503，而不是构造一个必然失败的执行器。"""
    from lyra.infrastructure.agents.genkit_runtime import (
        create_executor_from_environment,
    )

    assert create_executor_from_environment(
        connection=None, environment={}, tool_definitions=()
    ) is None


def test_factory_builds_an_executor_when_configured() -> None:
    """配置齐全时装配出生效的执行器。"""
    from lyra.infrastructure.agents.genkit_runtime import (
        GenkitTurnExecutor,
        create_executor_from_environment,
    )

    executor = create_executor_from_environment(
        connection=None,
        environment={
            "LYRA_GENKIT_BASE_URL": "https://models.example.com/v1",
            "LYRA_GENKIT_MODEL": "gpt-5",
            "LYRA_GENKIT_API_KEY": "sk-test",
        },
        tool_definitions=(),
    )

    assert isinstance(executor, GenkitTurnExecutor)


def test_factory_rejects_partial_configuration() -> None:
    """部分配置必须抛错，让服务在启动时失败而不是第一次对话时失败。"""
    from lyra.infrastructure.agents.genkit_runtime import (
        create_executor_from_environment,
    )

    with pytest.raises(ValueError, match="缺少"):
        create_executor_from_environment(
            connection=None,
            environment={"LYRA_GENKIT_BASE_URL": "https://models.example.com/v1"},
            tool_definitions=(),
        )


class _EchoDefinition:
    """一个把参数原样返回的工具。

    名字用 **真实白名单工具名**：系统上下文会拒绝非白名单工具，那是有意设计的
    边界（防止往提示词里注入任意工具名），不是可以绕过的细节。
    """

    name = "summary"
    description = "回显"
    input_schema = {"type": "object", "properties": {"value": {"type": "integer"}}}

    def __init__(self) -> None:
        self.calls: list = []

    def handler(self, arguments):
        self.calls.append(arguments)
        return {"echo": arguments}


def test_executor_runs_a_tool_and_returns_its_result() -> None:
    """执行器通过已装配的工具定义执行调用。"""
    from lyra.infrastructure.agents.genkit_runtime import (
        GenkitTurnExecutor,
        ModelChunk,
        ModelToolRequest,
    )

    definition = _EchoDefinition()
    stream = FakeStream(
        chunks=[ModelChunk(text="ok")], final="ok",
        tools=[ModelToolRequest(name="summary", arguments={"value": 3})],
    )
    executor = GenkitTurnExecutor(
        config=object(),
        tool_definitions=(definition,),
        stream_provider=lambda **kwargs: stream,
        execute_tool=lambda name, arguments: definition.handler(arguments),
    )

    signals = executor.run(session=_fake_session(), message_id="m", content="x")

    assert definition.calls == [{"value": 3}]
    assert any(isinstance(signal, MessageSnapshot) for signal in signals)


def _fake_session():
    """构造一个最小会话快照。"""
    from lyra.domain.agent.switching import AgentSessionSnapshot
    from lyra.domain.analytics.types import AccountScope

    return AgentSessionSnapshot(
        id="s-1", provider="genkit", status="running",
        account_scope=AccountScope(kind="all"), ledger_version=3, as_of="t",
        revision=1, summary=None, messages=(),
    )


def test_execute_returns_stable_error_for_unknown_tool() -> None:
    """未知工具返回稳定错误码而不是抛错——抛错会让整个回合失败。"""
    from lyra.infrastructure.agents.genkit_runtime import _execute

    assert _execute((), "nope", {}) == {"error": "TOOL_NOT_FOUND"}


def test_execute_returns_stable_error_for_invalid_arguments() -> None:
    """参数非法同样返回稳定错误码，让模型看到调用未成功并自行调整。"""
    from lyra.infrastructure.agents.genkit_runtime import _execute

    definition = _EchoDefinition()
    assert _execute((definition,), "summary", {"value": "not-an-integer"}) == {
        "error": "TOOL_ARGUMENTS_INVALID"
    }
    assert definition.calls == []


def test_execute_does_not_leak_handler_exceptions() -> None:
    """处理器异常必须折叠——细节只进日志，不回传模型。"""
    from lyra.infrastructure.agents.genkit_runtime import _execute

    class Exploding(_EchoDefinition):
        def handler(self, arguments):
            raise RuntimeError("postgres://user:pw@host")

    assert _execute((Exploding(),), "summary", {"value": 1}) == {
        "error": "TOOL_EXECUTION_FAILED"
    }
