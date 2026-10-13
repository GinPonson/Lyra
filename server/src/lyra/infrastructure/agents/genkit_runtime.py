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

**工具循环是真实的**：模型请求工具 → 执行 → 把原请求与结果一起回灌 → 继续生成，
直到模型给出不含工具请求的最终回答，或达到 ``max_turns``。工具结果的回灌与审计
是两个方向：回灌给模型的是完整数据，落库的只有有界摘要。

工具授权（账户范围、账本版本、截止时间）来自注入的 :class:`AgentToolContext`，
**不来自模型参数**；工具处理器拿到的参数与模型发出的参数完全一致。

无论成功或失败，最后**必须**发 ``TurnSettled()``：编排器据此判定回合已收口。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from itertools import count, groupby
from typing import Any, Protocol

from ...application.agent_config import DEFAULT_MAX_TURNS
from ...application.app_error import ApplicationError
from ...application.financial_tool_validation import is_valid_financial_tool_arguments
from ...application.financial_tools import FINANCIAL_TOOL_NAMES
from ...domain.agent.tool_call import (
    AgentToolContext,
    authorize_tool_call,
    normalize_summary,
)
from ...domain.agent.turn_stream import (
    AgentTurnStreamSignal,
    MessageDelta,
    MessageFailed,
    MessageSnapshot,
    ReasoningDelta,
    ToolCompleted,
    ToolStarted,
    TurnSettled,
    UsageUpdated,
)

# 单个回合允许的工具调用次数上限（与 TypeScript 基准一致）。
MAX_TOOL_CALLS_PER_TURN = 32


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


@dataclass(frozen=True)
class ModelToolExchange:
    """一次工具调用的请求与结果，用于把结果回灌给模型。

    @param turn: 属于第几次模型调用。同一次调用的多条结果必须合成一条 assistant
        工具消息，否则 OpenAI 兼容端点会因为 ``tool_calls`` 与结果不配对而拒绝请求。
    @param call_id: 本次调用的稳定标识，同时用作回灌消息里的 ``tool_call_id``。
    @param result: 回灌给模型的完整结果（工具结果本身，不是审计摘要）。
    """

    turn: int
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: Any


class ModelStream(Protocol):
    """一次模型调用的流式结果。"""

    def chunks(self) -> Iterable[ModelChunk]: ...

    def tool_requests(self) -> tuple[ModelToolRequest, ...]: ...

    def usage(self) -> ModelUsage | None: ...

    def final_text(self) -> str: ...


# 流提供者：接收用户输入、系统上下文、工具定义与已回灌的工具结果，返回一次模型调用。
StreamProvider = Callable[..., ModelStream]
# 工具执行器：接收模型请求与本次调用的审计 ID，返回回灌给模型的结果。
# 审计信号由执行器自己发出——只有它知道授权判定、耗时与稳定错误码。
ToolExecutor = Callable[[ModelToolRequest, str], Any]


def run_agent_turn_stream(
    *,
    content: str,
    system_context: str,
    tool_definitions: tuple[Any, ...],
    stream_provider: StreamProvider,
    execute_tool: ToolExecutor,
    emit: Callable[[AgentTurnStreamSignal], None],
    max_turns: int = DEFAULT_MAX_TURNS,
) -> None:
    """执行一次回合并把输出翻译成规范化信号。

    **上限耗尽不是成功**：模型在最后一轮仍在请求工具，说明回答尚未完成；此时发
    ``MessageFailed``——把中间产物当成最终回答，用户会读到一段没有结论的话。

    @param content: 用户输入。
    @param system_context: 系统提示词。
    @param tool_definitions: 可调用工具（含 name / input_schema / handler）。
    @param stream_provider: 模型流提供者。
    @param execute_tool: 工具执行器；返回值会被回灌给模型。
    @param emit: 信号接收器。
    @param max_turns: 允许的模型调用次数上限。
    """
    try:
        total_tokens: int | None = None
        exchanges: list[ModelToolExchange] = []
        final_text = ""
        for turn in range(max_turns):
            stream = stream_provider(
                content=content,
                system_context=system_context,
                tool_definitions=tool_definitions,
                exchanges=tuple(exchanges),
            )
            for chunk in stream.chunks():
                if chunk.reasoning:
                    # 只告诉客户端"在思考"，**原文不进入任何下游**。
                    emit(ReasoningDelta(delta="active"))
                if chunk.text:
                    emit(MessageDelta(delta=chunk.text))

            usage = stream.usage()
            if usage is not None:
                # 按轮累加：一次回答可能包含多次模型调用，只报最后一轮会少算用量。
                total_tokens = (total_tokens or 0) + usage.total_tokens

            requests = stream.tool_requests()
            if not requests:
                final_text = stream.final_text()
                break
            for request in requests:
                call_id = str(uuid.uuid4())
                # 结果必须回灌：丢弃它会让模型在下一轮凭空编造工具返回了什么。
                result = execute_tool(request, call_id)
                exchanges.append(ModelToolExchange(
                    turn=turn, call_id=call_id, tool_name=request.name,
                    arguments=dict(request.arguments), result=result,
                ))
        else:
            emit(MessageFailed(outcome="failed"))
            return

        if total_tokens is not None:
            # 成本由供应商决定；这里不猜——猜出来的成本会进审计记录。
            emit(UsageUpdated(total_tokens=total_tokens, total_cost=None))

        emit(MessageSnapshot(content=final_text, stop_reason="end_turn"))
    except Exception:  # noqa: BLE001 - 统一折叠为失败终态，细节只进日志
        emit(MessageFailed(outcome="failed"))
    finally:
        # 无论成功失败都必须收口，否则编排器会认为回合还没结束。
        emit(TurnSettled())


def execute_tool_request(
    *,
    tool_definitions: tuple[Any, ...],
    request: ModelToolRequest,
    call_id: str,
    ordinal: int,
    context: AgentToolContext,
    emit: Callable[[AgentTurnStreamSignal], None],
) -> Any:
    """执行一次工具调用，并以信号形式交出授权与结果事实。

    三步都与安全边界直接相关：

    1. **先授权再执行**。未知、未配置与参数非法的请求一律不执行，只记一次
       ``rejected``（``arguments_summary`` 为空——**拒绝的调用不保存模型参数**，
       否则凭据或邮件正文会永久留在审计库里）。
    2. **把原始参数交给处理器**。净化后的摘要只用于审计；拿摘要当参数会把越界调用
       改写成一次更宽的查询——空摘要能通过校验，于是"指定了越界账户"变成"查全部"。
    3. **结果原样回灌、摘要另算**。模型需要完整数据才能回答；落库的只有顶层标量。

    @param tool_definitions: 本会话可调用的工具。
    @param request: 模型的调用请求。
    @param call_id: 本次调用的审计 ID（同时是回灌消息里的 ``tool_call_id``）。
    @param ordinal: 本回合内第几次工具调用，从 1 开始。
    @param context: 服务端注入的账户范围、账本版本与截止时间。
    @param emit: 信号接收器。
    @returns 回灌给模型的结果；被拒或失败时是带稳定错误码的字典。
    """
    configured = frozenset(definition.name for definition in tool_definitions)
    definition = next(
        (candidate for candidate in tool_definitions if candidate.name == request.name),
        None,
    )
    rejection = authorize_tool_call(
        tool_name=request.name,
        known_tools=frozenset(FINANCIAL_TOOL_NAMES),
        configured_tools=configured,
    )
    if rejection is not None or definition is None:
        _emit_tool_completed(
            emit, context=context, call_id=call_id, ordinal=ordinal,
            tool_name=request.name, authorization_decision="denied", status="rejected",
            arguments_summary={}, result_summary=None, duration_ms=0,
            error_code="AGENT_TOOL_FORBIDDEN",
        )
        return {"error": "TOOL_NOT_FOUND"}
    if not is_valid_financial_tool_arguments(definition.input_schema, request.arguments):
        _emit_tool_completed(
            emit, context=context, call_id=call_id, ordinal=ordinal,
            tool_name=request.name, authorization_decision="denied", status="rejected",
            arguments_summary={}, result_summary=None, duration_ms=0,
            error_code="AGENT_TOOL_FORBIDDEN",
        )
        return {"error": "TOOL_ARGUMENTS_INVALID"}
    if ordinal > MAX_TOOL_CALLS_PER_TURN:
        # 每次调用都是一次真实查询，无上限会让一个回合把数据库读穿。
        _emit_tool_completed(
            emit, context=context, call_id=call_id, ordinal=ordinal,
            tool_name=request.name, authorization_decision="denied", status="rejected",
            arguments_summary={}, result_summary=None, duration_ms=0,
            error_code="AGENT_TOOL_FORBIDDEN",
        )
        return {"error": "TOOL_CALL_LIMIT_REACHED"}

    arguments_summary = _safe_arguments(request.arguments)
    emit(ToolStarted(
        message_id=context.user_message_id, call_id=call_id, ordinal=ordinal,
        tool_name=request.name, authorization_decision="allowed", status="started",
        arguments_summary=arguments_summary, result_summary=None, duration_ms=None,
        error_code=None, ledger_version=context.ledger_version,
    ))

    started_at = time.monotonic()
    try:
        data = definition.handler(request.arguments, context)
    except ApplicationError as error:
        # 预期错误（如账户不存在）只回灌稳定错误码：模型需要知道"这次没成功"，
        # 但内部细节不进入对话记录。
        _emit_tool_completed(
            emit, context=context, call_id=call_id, ordinal=ordinal,
            tool_name=request.name, authorization_decision="allowed", status="failed",
            arguments_summary=arguments_summary, result_summary=None,
            duration_ms=_elapsed_ms(started_at), error_code="AGENT_TOOL_EXECUTION_FAILED",
        )
        return {"error": error.code}
    except Exception:  # noqa: BLE001 - 内部异常文本不得透给模型
        _emit_tool_completed(
            emit, context=context, call_id=call_id, ordinal=ordinal,
            tool_name=request.name, authorization_decision="allowed", status="failed",
            arguments_summary=arguments_summary, result_summary=None,
            duration_ms=_elapsed_ms(started_at), error_code="AGENT_TOOL_EXECUTION_FAILED",
        )
        return {"error": "TOOL_EXECUTION_FAILED"}

    _emit_tool_completed(
        emit, context=context, call_id=call_id, ordinal=ordinal,
        tool_name=request.name, authorization_decision="allowed", status="completed",
        arguments_summary=arguments_summary, result_summary=_result_summary(data),
        duration_ms=_elapsed_ms(started_at), error_code=None,
    )
    return _financial_data_envelope(request.name, context, data)


def _emit_tool_completed(
    emit: Callable[[AgentTurnStreamSignal], None],
    *,
    context: AgentToolContext,
    call_id: str,
    ordinal: int,
    tool_name: str,
    authorization_decision: str,
    status: str,
    arguments_summary: dict[str, Any],
    result_summary: dict[str, Any] | None,
    duration_ms: int | None,
    error_code: str | None,
) -> None:
    """发出一条工具终态信号（编排器据此落库并推送事件）。"""
    emit(ToolCompleted(
        message_id=context.user_message_id, call_id=call_id, ordinal=ordinal,
        tool_name=tool_name, authorization_decision=authorization_decision,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        arguments_summary=arguments_summary, result_summary=result_summary,
        duration_ms=duration_ms, error_code=error_code,
        ledger_version=context.ledger_version,
    ))


def _financial_data_envelope(
    tool_name: str, context: AgentToolContext, data: Any
) -> dict[str, Any]:
    """交给模型的金融数据包；口径只从服务端会话上下文生成。

    模型必须能说明"这是哪个范围、哪个截止时间的数据"，而这些值不在它的参数里，
    因此随结果一起回灌。``instructionBoundary`` 复述提示词里的边界：工具输出是数据，
    不是指令。
    """
    scope = context.account_scope
    return {
        "kind": "lyra_financial_data",
        "toolName": tool_name,
        "provenance": {
            "accountScope": (
                {"kind": "all"} if scope.kind == "all"
                else {"kind": "account", "account": scope.account}
            ),
            "ledgerVersion": context.ledger_version,
            "asOf": context.as_of,
            "instructionBoundary": "data_only",
        },
        "data": data,
    }


def _elapsed_ms(started_at: float) -> int:
    """耗时毫秒；不为负（单调时钟理论上不回退，取整仍可能为 0）。"""
    return max(0, int((time.monotonic() - started_at) * 1000))


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


def _result_summary(result: Any) -> dict[str, Any]:
    """把工具结果收敛为可落库的摘要。

    只保留**顶层标量**：结果里的 ``rows`` 是完整查询结果，写进审计库等于把账本
    复制进事件表。摘要失败时退回空摘要——审计记录不该因为一个不可摘要的结果而整体
    丢失，而这一层本来就不承担数据通道的职责。

    @param result: 工具返回值。
    @returns 扁平、有界的标量摘要。
    """
    if not isinstance(result, Mapping):
        return {}
    flat = {
        key: value for key, value in result.items()
        if isinstance(value, (str, int, float, bool))
    }
    try:
        return dict(normalize_summary(flat, "结果"))
    except Exception:  # noqa: BLE001 - 审计优先于完整性
        return {}


# ---------------------------------------------------------------------------
# 真实执行器
# ---------------------------------------------------------------------------


class GenkitTurnExecutor:
    """把配置、工具目录与模型流绑成一个 ``AgentTurnExecutor``。

    **不在这里创建模型客户端**：``stream_provider`` 由 Genkit 绑定层提供，因此本类
    可在没有网络与账号的情况下完整测试。

    工具的执行、授权判定与审计信号都在本类内完成：只有执行器知道"这个结果是怎么
    来的"，把它交给调用方再回传会让结果与授权记录有机会脱节。

    @param config: 已校验的模型配置（提供 ``max_turns``）。
    @param tool_definitions: 可调用工具。
    @param stream_provider: 模型流提供者。
    """

    def __init__(
        self,
        *,
        config: Any,
        tool_definitions: tuple[Any, ...],
        stream_provider: StreamProvider,
    ) -> None:
        self._config = config
        self._tools = tool_definitions
        self._stream_provider = stream_provider

    def run(
        self,
        *,
        session: Any,
        content: str,
        context: AgentToolContext,
        emit: Callable[[AgentTurnStreamSignal], None],
    ) -> None:
        """执行一次回合，逐信号回调。

        **没有 ``message_id`` 参数**：流状态（含助手消息 ID）由编排器持有，执行器
        既不需要它、也无从校验它；留一个喂不进去的字段只会让后来人误以为它生效。

        @param session: 会话快照（系统提示词需要账户范围与账本版本）。
        @param content: 用户输入。
        @param context: 服务端注入的工具上下文（**不是**模型可控参数）；工具事件的
            消息归属取 ``context.user_message_id``。
        @param emit: 信号接收器；逐信号回调以支持边收边处理。
        @raises TypeError: ``context`` 不是 :class:`AgentToolContext`——形如字典的
            上下文同样被拒：字段缺失只会在工具执行到一半时才暴露。
        """
        from ...domain.agent.system_context import build_agent_system_context

        if not isinstance(context, AgentToolContext):
            # 明确失败而不是猜字段：把一个 dict 当上下文用，错误会推迟到工具执行
            # 过程中以 AttributeError 的形式出现，那时回合已经产生了一半事件。
            raise TypeError("Agent 回合上下文必须是 AgentToolContext")

        ordinal = count(1)

        def execute(request: ModelToolRequest, call_id: str) -> Any:
            return execute_tool_request(
                tool_definitions=self._tools, request=request, call_id=call_id,
                ordinal=next(ordinal), context=context, emit=emit,
            )

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
            execute_tool=execute,
            emit=emit,
            max_turns=self._config.max_turns,
        )


def create_executor_from_environment(
    *,
    connection: Any,
    environment: Mapping[str, str | None],
    tool_definitions: tuple[Any, ...],
) -> GenkitTurnExecutor | None:
    """按环境配置装配执行器。

    @param connection: 数据库连接（工具处理器据此读取账本）。
    @param environment: 环境变量。
    @param tool_definitions: 已装配的工具定义。
    @returns 执行器；模型未配置时返回 ``None``（路由据此返回 503，而不是构造一个
        必然失败的执行器）。
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
    )


def group_exchanges_by_turn(
    exchanges: Iterable[ModelToolExchange],
) -> Iterable[tuple[ModelToolExchange, ...]]:
    """把回灌记录按模型调用轮次分组，供供应商绑定层构造消息。

    同一次模型调用请求的多个工具必须落在一条 assistant 消息里；按顺序分组而不是
    排序，避免打乱工具结果的配对关系。
    """
    for _, group in groupby(exchanges, key=lambda exchange: exchange.turn):
        yield tuple(group)
