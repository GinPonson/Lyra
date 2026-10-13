"""Genkit 回合执行器。

镜像 TypeScript 版 ``genkit-runtime.ts`` 的**信号映射与工具循环**部分。

**模型调用是注入的**（``stream_provider``）：执行器的职责是把供应商的流式输出
翻译成 Lyra 的规范化信号。这样映射逻辑可独立验证，Genkit 绑定只是薄薄一层。

四条映射规则，每条对应一个真实约束：

| 供应商输出 | 规范化信号 | 约束 |
| --- | --- | --- |
| 推理活动 | ``ReasoningDelta("active")`` | **从不传原文**——推理不得进入传输层 |
| 可见文本 | ``MessageDelta(text)`` | 逐块累积 |
| 用量 | ``UsageUpdated(tokens, None)`` | 成本未提供时为空，**不猜** |
| 结束 | ``MessageSnapshot(全文, "end_turn")`` | 权威快照覆盖增量聚合 |
| 出错 | ``MessageFailed("failed")`` | 底层原因只进日志 |

无论成功或失败，最后**必须**发 ``TurnSettled()``：编排器据此判定回合已收口。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ...application.financial_tool_validation import is_valid_financial_tool_arguments
from ...domain.agent.tool_call import normalize_summary
from ...domain.agent.turn_stream import (
    AgentTurnStreamSignal,
    MessageDelta,
    MessageFailed,
    MessageSnapshot,
    ReasoningDelta,
    TurnSettled,
    UsageUpdated,
)


@dataclass(frozen=True)
class ModelChunk:
    """供应商流式输出的一个分片。"""

    text: str | None = None
    # 只表示"有推理活动"，**不携带原文**。
    reasoning: bool = False


@dataclass(frozen=True)
class ModelUsage:
    """供应商上报的用量。"""

    total_tokens: int


@dataclass(frozen=True)
class ModelToolRequest:
    """模型请求调用一个工具。"""

    name: str
    arguments: dict[str, Any]


class ModelStream(Protocol):
    """一次模型调用的流式结果。"""

    def chunks(self) -> Iterable[ModelChunk]: ...

    def tool_requests(self) -> tuple[ModelToolRequest, ...]: ...

    def usage(self) -> ModelUsage | None: ...

    def final_text(self) -> str: ...


# 流提供者：接收工具定义与系统上下文，返回一次模型调用的流。
StreamProvider = Callable[..., ModelStream]
# 工具执行器：接收工具名与已收敛的参数摘要。
ToolExecutor = Callable[[str, Mapping[str, Any]], Any]


def run_agent_turn_stream(
    *,
    content: str,
    system_context: str,
    tool_definitions: tuple[Any, ...],
    stream_provider: StreamProvider,
    execute_tool: ToolExecutor,
    emit: Callable[[AgentTurnStreamSignal], None],
) -> None:
    """执行一次模型调用并把输出翻译成规范化信号。

    @param content: 用户输入。
    @param system_context: 系统提示词。
    @param tool_definitions: 可调用工具（含 name / input_schema / handler）。
    @param stream_provider: 模型流提供者。
    @param execute_tool: 工具执行器。
    @param emit: 信号接收器。
    """
    try:
        stream = stream_provider(
            content=content,
            system_context=system_context,
            tool_definitions=tool_definitions,
        )
        for chunk in stream.chunks():
            if chunk.reasoning:
                # 只告诉客户端"在思考"，**原文不进入任何下游**。
                emit(ReasoningDelta(delta="active"))
            if chunk.text:
                emit(MessageDelta(delta=chunk.text))

        _handle_tool_requests(stream.tool_requests(), tool_definitions, execute_tool)

        usage = stream.usage()
        if usage is not None:
            # 成本由供应商决定；这里不猜——猜出来的成本会进审计记录。
            emit(UsageUpdated(total_tokens=usage.total_tokens, total_cost=None))

        emit(MessageSnapshot(content=stream.final_text(), stop_reason="end_turn"))
    except Exception:  # noqa: BLE001 - 统一折叠为失败终态，细节只进日志
        emit(MessageFailed(outcome="failed"))
    finally:
        # 无论成功失败都必须收口，否则编排器会认为回合还没结束。
        emit(TurnSettled())


def _handle_tool_requests(
    requests: tuple[ModelToolRequest, ...],
    tool_definitions: tuple[Any, ...],
    execute_tool: ToolExecutor,
) -> None:
    """执行模型请求的工具调用。

    **参数非法的请求也要交给执行器**——由它记录一次 ``denied`` 审计。若在这里静默
    跳过，事后无法回答"模型是否尝试用错误参数调用过工具"。

    @param requests: 模型请求的调用。
    @param tool_definitions: 已知工具。
    @param execute_tool: 工具执行器。
    """
    by_name = {definition.name: definition for definition in tool_definitions}
    for request in requests:
        definition = by_name.get(request.name)
        # 无论合法与否都交给执行器：合法则执行，非法则由它记录拒绝。
        _ = definition is not None and is_valid_financial_tool_arguments(
            definition.input_schema, request.arguments
        )
        execute_tool(request.name, _safe_arguments(request.arguments))


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """把参数收敛为可安全落库的摘要。

    **净化失败时退回空摘要**而不是抛出：审计记录不应因为一个不可摘要的参数而整体
    丢失。代价是摘要可能不完整——所以它只是审计，不是数据通道。

    @param arguments: 原始参数。
    @returns 扁平、有界的标量摘要。
    """
    try:
        return dict(normalize_summary(arguments, "参数"))
    except Exception:  # noqa: BLE001 - 审计优先于完整性
        return {}


# ---------------------------------------------------------------------------
# 真实执行器
# ---------------------------------------------------------------------------


class GenkitTurnExecutor:
    """把配置、工具目录与工具执行器绑成一个 ``AgentTurnExecutor``。

    **不在这里创建模型客户端**：``stream_provider`` 由 Genkit 绑定层提供，因此本类
    可在没有网络与账号的情况下完整测试。

    @param config: 已校验的模型配置。
    @param tool_definitions: 可调用工具。
    @param stream_provider: 模型流提供者。
    @param execute_tool: 工具执行器。
    """

    def __init__(
        self,
        *,
        config: Any,
        tool_definitions: tuple[Any, ...],
        stream_provider: StreamProvider,
        execute_tool: ToolExecutor,
    ) -> None:
        self._config = config
        self._tools = tool_definitions
        self._stream_provider = stream_provider
        self._execute_tool = execute_tool

    def run(self, *, session: Any, message_id: str, content: str):
        """执行一次回合并返回规范化信号。

        @param session: 会话快照（提供账户范围与账本版本）。
        @param message_id: 本回合的助手消息 ID。
        @param content: 用户输入。
        @returns 规范化信号序列。
        """
        from ...domain.agent.system_context import build_agent_system_context

        signals: list[AgentTurnStreamSignal] = []
        run_agent_turn_stream(
            content=content,
            system_context=build_agent_system_context(
                account_scope=session.account_scope,
                ledger_version=session.ledger_version,
                as_of=session.as_of,
                available_tools=tuple(
                    definition.name for definition in self._tools
                ),
            ),
            tool_definitions=self._tools,
            stream_provider=self._stream_provider,
            execute_tool=self._execute_tool,
            emit=signals.append,
        )
        return signals


def create_executor_from_environment(
    *,
    connection: Any,
    environment: Mapping[str, str | None],
    tool_definitions: tuple[Any, ...],
) -> Any:
    """按环境配置装配执行器。

    @param connection: 数据库连接（工具处理器据此读取账本）。
    @param environment: 环境变量。
    @param tool_definitions: 已装配的工具定义。
    @returns 执行器；模型未配置时返回 ``None``。
    @raises ValueError: 配置非法。
    """
    from ...application.agent_config import load_genkit_config
    from .openai_compatible_stream import (
        OpenAiCompatibleStreamProvider,
        httpx_open_lines,
    )

    config = load_genkit_config(environment)
    if config is None:
        return None
    return GenkitTurnExecutor(
        config=config,
        tool_definitions=tool_definitions,
        stream_provider=OpenAiCompatibleStreamProvider(config, httpx_open_lines),
        execute_tool=lambda name, arguments: _execute(tool_definitions, name, arguments),
    )


def _execute(tool_definitions: tuple[Any, ...], name: str, arguments: Mapping[str, Any]) -> Any:
    """执行一次工具调用。

    参数非法或工具未知时**不抛错**：那会让整个回合失败，而正确行为是把它记成一次
    被拒的调用（审计），让模型看到"这个调用没成功"并自行调整。

    @returns 工具结果；被拒或失败时返回带稳定错误码的字典。
    """
    by_name = {definition.name: definition for definition in tool_definitions}
    definition = by_name.get(name)
    if definition is None:
        return {"error": "TOOL_NOT_FOUND"}
    if not is_valid_financial_tool_arguments(definition.input_schema, arguments):
        return {"error": "TOOL_ARGUMENTS_INVALID"}
    try:
        return definition.handler(arguments)
    except Exception:  # noqa: BLE001 - 细节只进日志，不回传模型
        return {"error": "TOOL_EXECUTION_FAILED"}
