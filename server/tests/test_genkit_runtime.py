"""Genkit 执行器信号映射、工具循环与工具审计测试。

最要紧的一条是 :func:`test_reasoning_never_emits_the_original_text`：
供应商的推理活动只映射为 `ReasoningDelta("active")`，**原文不进入任何下游**。

另一条同样重要：**无论成功失败都必须发 `TurnSettled()`**。少了它，编排器会认为
回合还没结束，会话永远停在 running。

工具部分有三条边界，每条都对应一种真实故障：

1. 结果不回灌 → 模型在下一轮凭空编造工具返回了什么；
2. 净化后的摘要当参数 → 越界调用被静默改写成一次更宽的查询；
3. 轮次耗尽当成功 → 用户拿到一段没有结论的中间文本。
"""

from __future__ import annotations

import pytest

from lyra.application.agent_config import GenkitConfig
from lyra.domain.agent.tool_call import AgentToolContext
from lyra.domain.agent.turn_stream import (
    MessageDelta,
    MessageFailed,
    MessageSnapshot,
    ReasoningDelta,
    ToolCompleted,
    ToolStarted,
    TurnSettled,
    UsageUpdated,
)
from lyra.domain.analytics.types import AccountScope
from lyra.infrastructure.agents.genkit_runtime import (
    ModelChunk,
    ModelToolExchange,
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


class ScriptedProvider:
    """按顺序返回多个流的提供者，并记录每次调用收到的参数。"""

    def __init__(self, streams) -> None:
        self._streams = list(streams)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        assert self._streams, "模型调用次数超过了脚本预设"
        return self._streams.pop(0)


def _run(stream=None, *, tools=(), execute=None, captured=None, provider=None, max_turns=10):
    """执行一次回合并返回发出的信号。

    单流场景直接传 ``stream``；多轮工具循环传 ``provider``。
    """
    signals: list = []
    calls: list = []
    run_agent_turn_stream(
        content="这个月赚了多少",
        system_context="系统提示",
        tool_definitions=tuple(tools),
        stream_provider=provider or (lambda **kwargs: stream),
        execute_tool=execute or (
            lambda request, call_id: calls.append((request.name, request.arguments))
        ),
        emit=signals.append,
        max_turns=max_turns,
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


def test_usage_accumulates_across_turns() -> None:
    """一次回答包含多次模型调用时，用量按轮累加——只报最后一轮会少算。"""
    provider = ScriptedProvider([
        FakeStream(
            tools=[ModelToolRequest(name="summary", arguments={})],
            usage=ModelUsage(total_tokens=10),
        ),
        FakeStream(chunks=[ModelChunk(text="ok")], final="ok", usage=ModelUsage(total_tokens=5)),
    ])

    signals = _run(provider=provider, execute=lambda request, call_id: {"count": 1})

    usage = [signal for signal in signals if isinstance(signal, UsageUpdated)]
    assert usage == [UsageUpdated(total_tokens=15, total_cost=None)]


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
# 工具循环
# --------------------------------------------------------------------------


class FakeDefinition:
    """最小工具定义。"""

    def __init__(self, name: str, schema: dict) -> None:
        self.name = name
        self.input_schema = schema

    def handler(self, arguments, context):  # pragma: no cover - 本组测试不执行
        raise AssertionError("本组测试不应执行处理器")


def test_tool_loop_feedback_and_original_arguments() -> None:
    """**工具结果必须回灌**，并且模型请求的参数原样到达执行器。"""
    definition = FakeDefinition("summary", {"type": "object", "properties": {}})
    provider = ScriptedProvider([
        FakeStream(
            chunks=[ModelChunk(text="先查")], final="先查",
            tools=[ModelToolRequest(name="summary", arguments={"account": "TST001"})],
        ),
        FakeStream(chunks=[ModelChunk(text="答案")], final="答案"),
    ])
    calls: list = []

    signals = _run(provider=provider, tools=[definition], captured=calls)

    assert calls == [("summary", {"account": "TST001"})]
    assert len(provider.calls) == 2, "工具执行后必须继续生成"
    # 第二轮模型调用拿到了上一轮的调用与结果。
    exchanges = provider.calls[1]["exchanges"]
    assert [exchange.tool_name for exchange in exchanges] == ["summary"]
    assert exchanges[0].arguments == {"account": "TST001"}
    assert snapshots(signals)[-1].content == "答案"


def test_invalid_arguments_are_still_forwarded_for_audit() -> None:
    """**参数非法也要转发**——否则事后无法回答"模型是否尝试过错误调用"。"""
    definition = FakeDefinition(
        "summary", {"type": "object", "properties": {"n": {"type": "integer"}}}
    )
    provider = ScriptedProvider([
        FakeStream(
            tools=[ModelToolRequest(name="summary", arguments={"n": "not-an-integer"})],
        ),
        FakeStream(chunks=[ModelChunk(text="ok")], final="ok"),
    ])
    calls: list = []

    _run(provider=provider, tools=[definition], captured=calls)

    assert len(calls) == 1
    assert calls[0][0] == "summary"


def test_unknown_tool_is_still_forwarded_for_audit() -> None:
    """未知工具同样转发，由执行器记录拒绝。"""
    provider = ScriptedProvider([
        FakeStream(tools=[ModelToolRequest(name="nope", arguments={})]),
        FakeStream(chunks=[ModelChunk(text="ok")], final="ok"),
    ])
    calls: list = []

    _run(provider=provider, tools=[], captured=calls)

    assert calls == [("nope", {})]


def test_tool_loop_fails_when_max_turns_is_exhausted() -> None:
    """**轮次耗尽仍有工具请求不是成功**——把中间产物当答案是更糟的错误。"""
    definition = FakeDefinition("summary", {"type": "object", "properties": {}})
    provider = ScriptedProvider([
        FakeStream(tools=[ModelToolRequest(name="summary", arguments={})]),
        FakeStream(tools=[ModelToolRequest(name="summary", arguments={})]),
    ])

    signals = _run(provider=provider, tools=[definition], max_turns=2)

    assert len(provider.calls) == 2
    assert not snapshots(signals), "未完成的回答不得留下权威快照"
    assert any(isinstance(signal, MessageFailed) for signal in signals)
    assert isinstance(signals[-1], TurnSettled)


def test_exchange_rounds_group_tool_calls_for_replay() -> None:
    """同一次模型调用的多个工具必须归到同一轮，供绑定层配对 tool_calls 与结果。"""
    from lyra.infrastructure.agents.genkit_runtime import group_exchanges_by_turn

    exchanges = (
        ModelToolExchange(0, "c1", "summary", {}, {"count": 1}),
        ModelToolExchange(0, "c2", "positions", {}, {"count": 2}),
        ModelToolExchange(1, "c3", "trades", {}, {"count": 3}),
    )

    rounds = list(group_exchanges_by_turn(exchanges))

    assert [[exchange.call_id for exchange in round_] for round_ in rounds] == [
        ["c1", "c2"], ["c3"]
    ]


def test_executor_reuses_only_prior_conversation_history_for_each_tool_cycle() -> None:
    """工具循环每次请求都带同一份历史，且不混入本轮或系统/工具数据。"""
    from dataclasses import replace

    from lyra.domain.agent.switching import NormalizedAgentMessage

    prior_user = NormalizedAgentMessage(
        id="prior-user", role="user", content="重复问题", created_at="t1"
    )
    prior_assistant = NormalizedAgentMessage(
        id="prior-assistant", role="assistant", content="上一轮回答", created_at="t2"
    )
    session = replace(
        _fake_session(),
        messages=(
            prior_user,
            prior_assistant,
            NormalizedAgentMessage(
                id="old-system", role="system", content="旧系统内容", created_at="t3"
            ),
            NormalizedAgentMessage(
                id="old-tool", role="tool", content="旧工具数据", created_at="t4"
            ),
            NormalizedAgentMessage(
                id="u-1", role="user", content="重复问题", created_at="t5"
            ),
        ),
    )
    definition = _EchoDefinition()
    provider = ScriptedProvider([
        FakeStream(
            tools=[ModelToolRequest(name="summary", arguments={"value": 3})]
        ),
        FakeStream(chunks=[ModelChunk(text="回答")], final="回答"),
    ])
    executor = _executor(definition, provider, max_turns=2)
    signals: list = []

    executor.run(
        session=session, content="重复问题", context=_tool_context(), emit=signals.append
    )

    expected_history = (prior_user, prior_assistant)
    assert [call["history"] for call in provider.calls] == [
        expected_history, expected_history
    ]
    assert all(message.id != "u-1" for call in provider.calls for message in call["history"])
    assert definition.calls == [({"value": 3}, _tool_context())]


def test_cancellation_propagates_and_still_settles() -> None:
    """已取消回合向编排器透传异常，同时仍发出 TurnSettled。"""
    from lyra.application.agent_turn_orchestrator import AgentTurnCancelled

    signals: list = []

    def emit(signal) -> None:
        """模拟编排器在消费文本增量时发现已持久化的取消状态。"""
        if isinstance(signal, MessageDelta):
            raise AgentTurnCancelled()
        signals.append(signal)

    with pytest.raises(AgentTurnCancelled):
        run_agent_turn_stream(
            content="问题",
            system_context="系统提示",
            tool_definitions=(),
            stream_provider=lambda **_: FakeStream(
                chunks=[ModelChunk(text="部分答案")], final="部分答案"
            ),
            execute_tool=lambda request, call_id: None,
            emit=emit,
        )

    assert signals == [TurnSettled()]


def snapshots(signals: list) -> list:
    """取出权威快照信号。"""
    return [signal for signal in signals if isinstance(signal, MessageSnapshot)]


# --------------------------------------------------------------------------
# 摘要（审计面）与真实参数（数据面）
# --------------------------------------------------------------------------


def test_sensitive_arguments_are_stripped_from_the_summary() -> None:
    """**敏感字段不得进入审计摘要**，净化失败时退回空摘要。"""
    from lyra.infrastructure.agents.genkit_runtime import _safe_arguments

    summary = _safe_arguments({"accessToken": "sk-secret", "account": "TST001"})

    assert summary == {}
    assert "sk-secret" not in str(summary)


def test_unsafe_arguments_fall_back_to_empty_summary() -> None:
    """净化失败退回空摘要：审计记录不该因一个不可摘要的参数而整体丢失。"""
    from lyra.infrastructure.agents.genkit_runtime import _safe_arguments

    assert _safe_arguments({"payload": {"nested": 1}}) == {}


def test_result_summary_keeps_only_top_level_scalars() -> None:
    """结果摘要只保留顶层标量——``rows`` 是完整查询结果，不属于审计库。"""
    from lyra.infrastructure.agents.genkit_runtime import _result_summary

    assert _result_summary({"count": 2, "rows": [{"x": 1}], "aggregateCurrency": "USD"}) == {
        "count": 2, "aggregateCurrency": "USD"
    }


# --------------------------------------------------------------------------
# 执行器：授权、审计与参数
# --------------------------------------------------------------------------


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

    def handler(self, arguments, context):
        self.calls.append((arguments, context))
        return {"count": 1, "rows": [arguments]}


class _PermissiveDefinition(_EchoDefinition):
    """接受任意参数的工具，用来验证"摘要 ≠ 参数"。"""

    input_schema = {"type": "object", "properties": {}, "additionalProperties": True}


def _config(*, max_turns: int = 10) -> GenkitConfig:
    """构造执行器需要的最小已校验配置。"""
    return GenkitConfig(
        base_url="https://models.example.com/v1", model="gpt-5", api_key="sk-test",
        turn_timeout_ms=120_000, max_turns=max_turns,
    )


def _tool_context() -> AgentToolContext:
    """构造一个会话绑定的服务端工具上下文。"""
    return AgentToolContext(
        session_id="s-1", device_id="d-1", user_id=AUDIT_USER_ID, user_message_id="u-1",
        account_scope=AccountScope(kind="all"), ledger_version=3,
        as_of="2026-09-01T00:00:00.000Z",
    )


def _fake_session():
    """构造一个最小会话快照。"""
    from lyra.domain.agent.switching import AgentSessionSnapshot

    return AgentSessionSnapshot(
        id="s-1", provider="genkit", status="running",
        account_scope=AccountScope(kind="all"), ledger_version=3, as_of="t",
        revision=1, summary=None, messages=(),
    )


def _executor(definition, provider, *, max_turns: int = 10):
    """按新签名装配执行器。"""
    from lyra.infrastructure.agents.genkit_runtime import GenkitTurnExecutor

    return GenkitTurnExecutor(
        config=_config(max_turns=max_turns),
        tool_definitions=(definition,),
        stream_provider=provider,
    )


def _run_executor(executor) -> list:
    """执行一个回合并收集信号。"""
    signals: list = []
    executor.run(
        session=_fake_session(), content="这个月赚了多少",
        context=_tool_context(), emit=signals.append,
    )
    return signals


def test_executor_feeds_the_result_back_and_keeps_generating() -> None:
    """执行器完成一次真实工具循环：执行 → 回灌 → 继续生成。"""
    definition = _EchoDefinition()
    provider = ScriptedProvider([
        FakeStream(
            chunks=[ModelChunk(text="先查")], final="先查",
            tools=[ModelToolRequest(name="summary", arguments={"value": 3})],
        ),
        FakeStream(chunks=[ModelChunk(text="结果 3")], final="结果 3"),
    ])

    signals = _run_executor(_executor(definition, provider))

    assert definition.calls == [({"value": 3}, _tool_context())]
    replay = provider.calls[1]["exchanges"]
    # 回灌的是**完整工具结果**（含服务端口径），不是审计摘要。
    assert replay[0].result["kind"] == "lyra_financial_data"
    assert replay[0].result["data"] == {"count": 1, "rows": [{"value": 3}]}
    assert replay[0].result["provenance"]["ledgerVersion"] == 3
    assert replay[0].result["provenance"]["asOf"] == "2026-09-01T00:00:00.000Z"
    assert snapshots(signals)[-1].content == "结果 3"


def test_sanitized_summary_never_replaces_the_real_arguments() -> None:
    """**净化后的摘要只用于审计**：不能因为摘要退化成空，就少给处理器参数。

    这正是修复前的行为——空摘要能通过校验，于是"带了一个越界字段的调用"被静默
    改写成一次更宽的查询。
    """
    definition = _PermissiveDefinition()
    provider = ScriptedProvider([
        FakeStream(tools=[ModelToolRequest(
            name="summary", arguments={"payload": {"nested": 1}}
        )]),
        FakeStream(chunks=[ModelChunk(text="ok")], final="ok"),
    ])

    signals = _run_executor(_executor(definition, provider))

    assert definition.calls == [({"payload": {"nested": 1}}, _tool_context())]
    started = [signal for signal in signals if isinstance(signal, ToolStarted)]
    assert started[0].arguments_summary == {}, "不可摘要的参数只允许退化成空摘要"


def test_tool_audit_signals_describe_start_and_completion() -> None:
    """一次成功调用产出 ``ToolStarted`` 与 ``ToolCompleted``，且带稳定审计事实。"""
    definition = _EchoDefinition()
    provider = ScriptedProvider([
        FakeStream(tools=[ModelToolRequest(name="summary", arguments={"value": 3})]),
        FakeStream(chunks=[ModelChunk(text="ok")], final="ok"),
    ])

    signals = _run_executor(_executor(definition, provider))

    started = [signal for signal in signals if isinstance(signal, ToolStarted)]
    completed = [signal for signal in signals if isinstance(signal, ToolCompleted)]

    assert len(started) == 1 and len(completed) == 1
    assert started[0].message_id == "u-1", "工具调用挂靠已落库的用户消息"
    assert started[0].ordinal == 1
    assert started[0].authorization_decision == "allowed"
    assert started[0].status == "started"
    assert started[0].arguments_summary == {"value": 3}
    assert started[0].ledger_version == 3
    assert completed[0].call_id == started[0].call_id
    assert completed[0].status == "completed"
    assert completed[0].result_summary == {"count": 1}
    assert completed[0].error_code is None
    assert completed[0].duration_ms is not None and completed[0].duration_ms >= 0


def test_unknown_tool_request_is_rejected_without_executing() -> None:
    """未知工具：不执行、只记一次 ``rejected``，参数不进审计。"""
    definition = _EchoDefinition()
    provider = ScriptedProvider([
        FakeStream(tools=[ModelToolRequest(
            name="not-a-tool", arguments={"value": 3}
        )]),
        FakeStream(chunks=[ModelChunk(text="ok")], final="ok"),
    ])

    signals = _run_executor(_executor(definition, provider))

    assert definition.calls == []
    completed = [signal for signal in signals if isinstance(signal, ToolCompleted)]
    assert completed[0].status == "rejected"
    assert completed[0].authorization_decision == "denied"
    assert completed[0].error_code == "AGENT_TOOL_FORBIDDEN"
    assert completed[0].arguments_summary == {}, "拒绝的调用不得保存模型参数"
    assert not any(isinstance(signal, ToolStarted) for signal in signals)


def test_invalid_arguments_are_rejected_and_the_model_sees_a_stable_code() -> None:
    """参数非法：不执行，模型只拿到稳定错误码。"""
    definition = _EchoDefinition()
    provider = ScriptedProvider([
        FakeStream(tools=[ModelToolRequest(
            name="summary", arguments={"value": "not-an-integer"}
        )]),
        FakeStream(chunks=[ModelChunk(text="ok")], final="ok"),
    ])

    signals = _run_executor(_executor(definition, provider))

    assert definition.calls == []
    completed = [signal for signal in signals if isinstance(signal, ToolCompleted)]
    assert completed[0].status == "rejected"
    assert provider.calls[1]["exchanges"][0].result == {"error": "TOOL_ARGUMENTS_INVALID"}


def test_tool_call_limit_rejects_further_calls() -> None:
    """单回合工具调用次数有上限：无上限会让一个回合把数据库读穿。"""
    from lyra.infrastructure.agents.genkit_runtime import MAX_TOOL_CALLS_PER_TURN

    definition = _EchoDefinition()
    provider = ScriptedProvider([
        FakeStream(tools=[
            ModelToolRequest(name="summary", arguments={"value": index})
            for index in range(MAX_TOOL_CALLS_PER_TURN + 1)
        ]),
        FakeStream(chunks=[ModelChunk(text="ok")], final="ok"),
    ])

    signals = _run_executor(_executor(definition, provider))

    assert len(definition.calls) == MAX_TOOL_CALLS_PER_TURN
    completed = [signal for signal in signals if isinstance(signal, ToolCompleted)]
    assert completed[-1].status == "rejected"
    assert provider.calls[1]["exchanges"][-1].result == {"error": "TOOL_CALL_LIMIT_REACHED"}


def test_handler_failure_is_folded_and_does_not_leak_internals() -> None:
    """处理器异常折叠成稳定错误码，细节不回传模型。"""
    class Exploding(_EchoDefinition):
        def handler(self, arguments, context):
            raise RuntimeError("postgres://user:pw@host")

    provider = ScriptedProvider([
        FakeStream(tools=[ModelToolRequest(name="summary", arguments={"value": 1})]),
        FakeStream(chunks=[ModelChunk(text="ok")], final="ok"),
    ])

    signals = _run_executor(_executor(Exploding(), provider))

    completed = [signal for signal in signals if isinstance(signal, ToolCompleted)]
    assert completed[0].status == "failed"
    assert completed[0].error_code == "AGENT_TOOL_EXECUTION_FAILED"
    assert "postgres://" not in str(provider.calls[1]["exchanges"])


def test_expected_application_error_returns_its_stable_code() -> None:
    """预期错误（如账户不存在）回灌稳定错误码，让模型看到调用未成功。"""
    from lyra.application.app_error import ApplicationError

    class MissingAccount(_EchoDefinition):
        def handler(self, arguments, context):
            raise ApplicationError(
                code="ACCOUNT_NOT_FOUND", message="账户不存在",
                status_code=404, retryable=False,
            )

    provider = ScriptedProvider([
        FakeStream(tools=[ModelToolRequest(name="summary", arguments={"value": 1})]),
        FakeStream(chunks=[ModelChunk(text="ok")], final="ok"),
    ])

    signals = _run_executor(_executor(MissingAccount(), provider))

    completed = [signal for signal in signals if isinstance(signal, ToolCompleted)]
    assert completed[0].error_code == "AGENT_TOOL_EXECUTION_FAILED"
    assert provider.calls[1]["exchanges"][0].result == {"error": "ACCOUNT_NOT_FOUND"}


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


def test_factory_reads_max_turns_from_the_environment() -> None:
    """``LYRA_AGENT_MAX_TURNS`` 必须真正生效——它此前被解析后从未使用。"""
    from lyra.infrastructure.agents.genkit_runtime import (
        create_executor_from_environment,
    )

    definition = _EchoDefinition()
    executor = create_executor_from_environment(
        connection=None,
        environment={
            "LYRA_GENKIT_BASE_URL": "https://models.example.com/v1",
            "LYRA_GENKIT_MODEL": "gpt-5",
            "LYRA_GENKIT_API_KEY": "sk-test",
            "LYRA_AGENT_MAX_TURNS": "3",
        },
        tool_definitions=(definition,),
    )
    provider = ScriptedProvider([
        FakeStream(tools=[ModelToolRequest(name="summary", arguments={"value": 1})])
        for _ in range(3)
    ])
    executor._stream_provider = provider  # 仅替换模型流；轮次上限仍来自环境配置

    signals = _run_executor(executor)

    assert len(provider.calls) == 3, "轮次上限必须来自 LYRA_AGENT_MAX_TURNS"
    assert any(isinstance(signal, MessageFailed) for signal in signals)


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


# --------------------------------------------------------------------------
# 审计落盘契约
# --------------------------------------------------------------------------

AUDIT_ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"
AUDIT_DEVICE_ID = "22222222-2222-2222-2222-222222222222"
AUDIT_SESSION_ID = "33333333-3333-3333-3333-333333333333"
AUDIT_MESSAGE_ID = "44444444-4444-4444-4444-444444444444"
AUDIT_STARTED_AT = "2026-10-12T09:00:01.000Z"
AUDIT_USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
AUDIT_IDENTITY_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
AUDIT_COMPLETED_AT = "2026-10-12T09:00:02.000Z"


def _audit_repository(tmp_path):
    """装配一个可写 ``agent_tool_calls`` 的真实库（外键依赖用户消息已落库）。"""
    from datetime import UTC, datetime

    from lyra.db.connection import open_lyra_database
    from lyra.domain.agent.messages import start_agent_message
    from lyra.domain.agent.switching import (
        AgentProviderAvailability,
        create_agent_session,
    )
    from lyra.infrastructure.db.agent_message_repository import AgentMessageRepository
    from lyra.infrastructure.db.agent_session_repository import (
        DuckDbAgentSessionRepository,
    )

    database = open_lyra_database(str(tmp_path / "audit.duckdb"))
    connection = database.connection
    connection.execute(
        "INSERT INTO users (id, display_name) VALUES (CAST(? AS UUID), 'Owner')",
        [AUDIT_USER_ID],
    )
    connection.execute(
        """
        INSERT INTO user_identities (id, user_id, provider, subject)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), 'github', 'audit-subject')
        """,
        [AUDIT_IDENTITY_ID, AUDIT_USER_ID],
    )
    connection.execute(
        """
        INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Account', 'TST001', '001')
        """,
        [AUDIT_ACCOUNT_ID, AUDIT_USER_ID],
    )
    connection.execute(
        """
        INSERT INTO paired_devices
            (id, user_id, identity_id, display_name, public_key_pem,
             public_key_fingerprint, key_algorithm, status)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), CAST(? AS UUID), 'Pixel 9',
                '-----BEGIN PUBLIC KEY-----', ?, 'ec', 'active')
        """,
        [AUDIT_DEVICE_ID, AUDIT_USER_ID, AUDIT_IDENTITY_ID, "f" * 64],
    )
    DuckDbAgentSessionRepository(connection).create_session(
        create_agent_session(
            session_id=AUDIT_SESSION_ID, provider="genkit",
            availability=AgentProviderAvailability(provider="genkit", status="available"),
            account_scope=AccountScope(kind="all"), ledger_version=3,
            as_of="2026-09-01T00:00:00.000Z",
        ),
        device_id=AUDIT_DEVICE_ID, user_id=AUDIT_USER_ID,
        now=datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC),
    )
    repository = AgentMessageRepository(connection)
    repository.append_message(start_agent_message(
        message_id=AUDIT_MESSAGE_ID, session_id=AUDIT_SESSION_ID, ordinal=1,
        role="user", content="这个月赚了多少", created_at=AUDIT_STARTED_AT,
    ))
    return repository, database


def _audit_run(definition, provider) -> list:
    """在真实会话绑定下执行一个回合并返回信号。"""
    from lyra.domain.agent.switching import AgentSessionSnapshot

    executor = _executor(definition, provider)
    signals: list = []
    executor.run(
        session=AgentSessionSnapshot(
            id=AUDIT_SESSION_ID, provider="genkit", status="running",
            account_scope=AccountScope(kind="all"), ledger_version=3,
            as_of="2026-09-01T00:00:00.000Z", revision=1, summary=None, messages=(),
        ),
        content="这个月赚了多少",
        context=AgentToolContext(
            session_id=AUDIT_SESSION_ID, device_id=AUDIT_DEVICE_ID,
            user_id=AUDIT_USER_ID, user_message_id=AUDIT_MESSAGE_ID,
            account_scope=AccountScope(kind="all"),
            ledger_version=3, as_of="2026-09-01T00:00:00.000Z",
        ),
        emit=signals.append,
    )
    return signals


def test_tool_signals_are_enough_to_persist_a_completed_audit_row(tmp_path) -> None:
    """**信号字段足以落库**：编排器据此写出 ``agent_tool_calls`` 的完整记录。"""
    from lyra.domain.agent.messages import finish_tool_call, start_tool_call

    repository, database = _audit_repository(tmp_path)
    try:
        definition = _EchoDefinition()
        provider = ScriptedProvider([
            FakeStream(tools=[ModelToolRequest(name="summary", arguments={"value": 3})]),
            FakeStream(chunks=[ModelChunk(text="ok")], final="ok"),
        ])

        signals = _audit_run(definition, provider)
        started = [signal for signal in signals if isinstance(signal, ToolStarted)][0]
        completed = [signal for signal in signals if isinstance(signal, ToolCompleted)][0]

        # 编排器侧的动作：用信号里的字段构造领域记录并落库。
        record = start_tool_call(
            call_id=started.call_id, session_id=AUDIT_SESSION_ID,
            message_id=started.message_id, ordinal=started.ordinal,
            tool_name=started.tool_name, arguments_summary=started.arguments_summary,
            ledger_version=started.ledger_version, started_at=AUDIT_STARTED_AT,
        )
        repository.append_tool_call(record)
        finished = finish_tool_call(
            record, status=completed.status, result_summary=completed.result_summary,
            duration_ms=completed.duration_ms, error_code=completed.error_code,
            completed_at=AUDIT_COMPLETED_AT,
        )
        assert repository.complete_tool_call(finished) is True

        rows = repository.load_tool_calls(AUDIT_SESSION_ID)
        assert len(rows) == 1
        assert rows[0]["toolName"] == "summary"
        assert rows[0]["messageId"] == AUDIT_MESSAGE_ID
        assert rows[0]["authorizationDecision"] == "allowed"
        assert rows[0]["status"] == "completed"
        assert rows[0]["argumentsSummary"] == {"value": 3}
        assert rows[0]["resultSummary"] == {"count": 1}
        assert rows[0]["ledgerVersion"] == 3
    finally:
        database.close()


def test_tool_signals_are_enough_to_persist_a_rejection(tmp_path) -> None:
    """被拒的调用同样落库：事后必须能回答"模型试过调用什么"。"""
    from lyra.domain.agent.messages import reject_tool_call

    repository, database = _audit_repository(tmp_path)
    try:
        definition = _EchoDefinition()
        provider = ScriptedProvider([
            FakeStream(tools=[ModelToolRequest(name="not-a-tool", arguments={})]),
            FakeStream(chunks=[ModelChunk(text="ok")], final="ok"),
        ])

        signals = _audit_run(definition, provider)
        completed = [signal for signal in signals if isinstance(signal, ToolCompleted)][0]

        repository.append_tool_call(reject_tool_call(
            call_id=completed.call_id, session_id=AUDIT_SESSION_ID,
            message_id=completed.message_id, ordinal=completed.ordinal,
            tool_name=completed.tool_name.replace("not-a-tool", "invalid-tool-name"),
            arguments_summary=completed.arguments_summary,
            ledger_version=completed.ledger_version, rejected_at=AUDIT_STARTED_AT,
        ))

        rows = repository.load_tool_calls(AUDIT_SESSION_ID)
        assert rows[0]["authorizationDecision"] == "denied"
        assert rows[0]["status"] == "rejected"
        assert rows[0]["argumentsSummary"] == {}
    finally:
        database.close()


def test_executor_rejects_a_mapping_context() -> None:
    """上下文必须是 ``AgentToolContext``：形如字典的上下文只会在工具执行到一半时
    抛出 AttributeError，那时回合已经产生了一半事件。"""
    definition = _EchoDefinition()
    provider = ScriptedProvider([FakeStream(chunks=[ModelChunk(text="x")], final="x")])
    executor = _executor(definition, provider)

    with pytest.raises(TypeError, match="AgentToolContext"):
        executor.run(
            session=_fake_session(), content="x",
            context={"account_scope": AccountScope(kind="all")},  # type: ignore[arg-type]
            emit=lambda signal: None,
        )


def test_bound_session_cannot_reach_other_accounts_end_to_end(tmp_path) -> None:
    """**端到端授权边界**：会话绑定单账户时，用真实工具定义也拿不到别的账户。

    这是本次修复的核心：账户范围来自会话上下文，模型不传任何范围字段都不影响。
    """
    from lyra.application.financial_tool_definitions import build_tool_definitions
    from lyra.db.connection import open_lyra_database
    from lyra.domain.agent.switching import AgentSessionSnapshot
    from lyra.infrastructure.agents.genkit_runtime import GenkitTurnExecutor

    bound = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    other = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    user_a = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    user_b = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    database = open_lyra_database(str(tmp_path / "scope.duckdb"))
    try:
        connection = database.connection
        # 两个账户分属**两个用户**：否则"拿不到别的账户"可能只是因为同属一个用户。
        for user_id, display in ((user_a, "A"), (user_b, "B")):
            connection.execute(
                "INSERT INTO users (id, display_name) VALUES (CAST(? AS UUID), ?)",
                [user_id, display],
            )
        for account_id, key, owner in ((bound, "TST001", user_a), (other, "TST002", user_b)):
            connection.execute(
                """
                INSERT INTO accounts (id, user_id, display_name, legacy_account_key,
                                      masked_suffix)
                VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Account', ?, '001')
                """,
                [account_id, owner, key],
            )
        scope = AccountScope(kind="account", account=bound)
        provider = ScriptedProvider([
            FakeStream(tools=[
                ModelToolRequest(name="accounts", arguments={}),
                ModelToolRequest(name="summary", arguments={}),
            ]),
            FakeStream(chunks=[ModelChunk(text="ok")], final="ok"),
        ])
        executor = GenkitTurnExecutor(
            config=_config(),
            tool_definitions=build_tool_definitions(lambda: connection),
            stream_provider=provider,
        )

        executor.run(
            session=AgentSessionSnapshot(
                id="s-1", provider="genkit", status="running", account_scope=scope,
                ledger_version=3, as_of="2026-09-01T00:00:00.000Z", revision=1,
                summary=None, messages=(),
            ),
            content="列出我授权范围内的账户",
            context=AgentToolContext(
                session_id="s-1", device_id="d-1", user_id=user_a,
                user_message_id="u-1",
                account_scope=scope, ledger_version=3,
                as_of="2026-09-01T00:00:00.000Z",
            ),
            emit=lambda signal: None,
        )

        exchanges = provider.calls[1]["exchanges"]
        assert [row["id"] for row in exchanges[0].result["data"]["rows"]] == [bound]
        groups = exchanges[1].result["data"]["groups"]
        assert [group["accountIds"] for group in groups] == [[bound]]
    finally:
        database.close()
