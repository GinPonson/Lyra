"""Agent 消息与工具调用的生命周期。

数据库对这两张表有严格的状态—字段一致性约束（见 ``agent-genkit-schema.sql``）。
本模块把同一套规则实现在领域层，**在写入前就判定**，而不是等数据库抛出一个
`ConstraintException`——后者只有列名，没有"哪条规则被违反"。

三条一致性规则（与 schema 一一对应）：

| 状态 | 完成时间 | 错误码 |
| --- | --- | --- |
| `streaming` | 必须为空 | 必须为空 |
| `completed` | 必须存在 | 必须为空 |
| `failed` / `cancelled` | 必须存在 | 可空 |

**`completed` 不允许带错误码**这条尤其容易被忽略：一个"完成但报错"的消息在语义上
自相矛盾，客户端无从判断该显示成功还是失败。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

AgentMessageRole = Literal["user", "assistant", "tool", "system"]
AgentMessageStatus = Literal["streaming", "completed", "failed", "cancelled"]
ToolCallStatus = Literal["started", "completed", "failed", "cancelled", "rejected"]
AuthorizationDecision = Literal["allowed", "denied"]


@dataclass(frozen=True)
class AgentMessage:
    """一条会话消息。"""

    id: str
    session_id: str
    ordinal: int
    role: AgentMessageRole
    provider: Literal["genkit", "claude", "pi", "opencode"] | None
    status: AgentMessageStatus
    content: str
    error_code: str | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class AgentToolCall:
    """一次工具调用。"""

    id: str
    session_id: str
    message_id: str
    ordinal: int
    tool_name: str
    authorization_decision: AuthorizationDecision
    status: ToolCallStatus
    arguments_summary: dict[str, object]
    result_summary: dict[str, object] | None
    ledger_version: int
    duration_ms: int | None
    error_code: str | None
    started_at: str
    completed_at: str | None


def start_agent_message(
    *,
    message_id: str,
    session_id: str,
    ordinal: int,
    role: AgentMessageRole,
    content: str,
    created_at: str,
    provider: str | None = "genkit",
) -> AgentMessage:
    """开始一条消息（``streaming``）。

    @param message_id: 消息 ID。
    @param session_id: 所属会话。
    @param ordinal: 会话内序号，必须为正。
    @param role: 角色。
    @param content: 当前内容（流式过程中会增长）。
    @param created_at: 创建时间。
    @param provider: 生成该消息的供应商；用户与系统消息可为空。
    @returns 处于 ``streaming`` 的消息。
    @raises ValueError: 参数非法。
    """
    return AgentMessage(
        id=_require_text(message_id, "Agent 消息 ID"),
        session_id=_require_text(session_id, "Agent 会话 ID"),
        ordinal=_require_positive(ordinal, "Agent 消息序号"),
        role=role,
        provider=provider,
        status="streaming",
        content=content,
        error_code=None,
        created_at=_require_text(created_at, "Agent 消息创建时间"),
        completed_at=None,
    )


def finish_agent_message(
    message: AgentMessage,
    *,
    status: AgentMessageStatus,
    content: str | None = None,
    error_code: str | None = None,
    completed_at: str,
) -> AgentMessage:
    """终止一条消息。

    @param message: 进行中的消息。
    @param status: 终态，须为 ``completed`` / ``failed`` / ``cancelled``。
    @param content: 最终内容；``None`` 表示保持原内容。
    @param error_code: 失败或取消时的稳定错误码。
    @param completed_at: 完成时间。
    @returns 终止后的消息。
    @raises ValueError: 状态不是终态、消息已终止，或字段组合违反一致性规则。
    """
    if status == "streaming":
        raise ValueError("终止消息不能用 streaming 状态")
    if message.status != "streaming":
        raise ValueError(f"消息已处于终态 {message.status}，不能再次终止")
    _assert_consistency(status, completed_at, error_code)
    return replace(
        message,
        status=status,
        content=message.content if content is None else content,
        error_code=error_code,
        completed_at=completed_at,
    )


def start_tool_call(
    *,
    call_id: str,
    session_id: str,
    message_id: str,
    ordinal: int,
    tool_name: str,
    arguments_summary: dict[str, object],
    ledger_version: int,
    started_at: str,
) -> AgentToolCall:
    """开始一次**已获授权**的工具调用。

    被拒的调用不走这里——它从未开始，用 :func:`reject_tool_call`。

    @param call_id: 调用 ID。
    @param session_id: 所属会话。
    @param message_id: 触发该调用的消息。
    @param ordinal: 消息内序号，必须为正。
    @param tool_name: 工具名。
    @param arguments_summary: 参数摘要。
    @param ledger_version: 会话账本版本。
    @param started_at: 开始时间。
    @returns 处于 ``started`` 的调用记录。
    @raises ValueError: 参数非法。
    """
    return AgentToolCall(
        id=_require_text(call_id, "工具调用 ID"),
        session_id=_require_text(session_id, "Agent 会话 ID"),
        message_id=_require_text(message_id, "工具调用所属消息 ID"),
        ordinal=_require_positive(ordinal, "工具调用序号"),
        tool_name=_require_text(tool_name, "工具名"),
        authorization_decision="allowed",
        status="started",
        arguments_summary=dict(arguments_summary),
        result_summary=None,
        ledger_version=_require_non_negative(ledger_version, "工具调用账本版本"),
        duration_ms=None,
        error_code=None,
        started_at=_require_text(started_at, "工具调用开始时间"),
        completed_at=None,
    )


def reject_tool_call(
    *,
    call_id: str,
    session_id: str,
    message_id: str,
    ordinal: int,
    tool_name: str,
    arguments_summary: dict[str, object],
    ledger_version: int,
    rejected_at: str,
) -> AgentToolCall:
    """记录一次**被授权层拦下**的调用。

    被拒的调用**从未开始**，因此 ``status`` 直接是 ``rejected``，且没有耗时——
    数据库的 CHECK 也是这样要求的：``denied`` 必须配 ``rejected``。

    之所以仍要落库：它是审计的一部分。事后需要能回答"模型是否尝试过访问
    授权范围外的账户"。

    @param rejected_at: 判定为拒绝的时间，同时作为开始与完成时间。
    @returns 处于 ``rejected`` 的调用记录。
    @raises ValueError: 参数非法。
    """
    stamp = _require_text(rejected_at, "工具调用拒绝时间")
    return AgentToolCall(
        id=_require_text(call_id, "工具调用 ID"),
        session_id=_require_text(session_id, "Agent 会话 ID"),
        message_id=_require_text(message_id, "工具调用所属消息 ID"),
        ordinal=_require_positive(ordinal, "工具调用序号"),
        tool_name=_require_text(tool_name, "工具名"),
        authorization_decision="denied",
        status="rejected",
        arguments_summary=dict(arguments_summary),
        result_summary=None,
        ledger_version=_require_non_negative(ledger_version, "工具调用账本版本"),
        duration_ms=None,
        error_code=None,
        started_at=stamp,
        completed_at=stamp,
    )


def finish_tool_call(
    call: AgentToolCall,
    *,
    status: ToolCallStatus,
    result_summary: dict[str, object] | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
    completed_at: str,
) -> AgentToolCall:
    """终止一次工具调用。

    @param call: 进行中的调用。
    @param status: 终态，须不是 ``started``。
    @param result_summary: 结果摘要；**不得包含完整结果或凭据**。
    @param duration_ms: 耗时毫秒。
    @param error_code: 稳定错误码。
    @param completed_at: 完成时间。
    @returns 终止后的记录。
    @raises ValueError: 状态不是终态或调用已终止。
    """
    if status == "started":
        raise ValueError("终止工具调用不能用 started 状态")
    if call.status != "started":
        raise ValueError(f"工具调用已处于终态 {call.status}，不能再次终止")
    return replace(
        call,
        status=status,
        result_summary=result_summary,
        duration_ms=duration_ms,
        error_code=error_code,
        completed_at=completed_at,
    )


def _assert_consistency(
    status: AgentMessageStatus, completed_at: str | None, error_code: str | None
) -> None:
    """校验状态与字段的一致性（与 schema 的 CHECK 同义）。

    @raises ValueError: 组合不合法。
    """
    if status == "streaming":
        if completed_at is not None or error_code is not None:
            raise ValueError("streaming 消息不能有完成时间或错误码")
        return
    if completed_at is None:
        raise ValueError(f"{status} 消息必须有完成时间")
    if status == "completed" and error_code is not None:
        # "完成但报错"在语义上自相矛盾，客户端无从判断该显示成功还是失败。
        raise ValueError("completed 消息不能带错误码")


def _require_text(value: str, label: str) -> str:
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{label}不能为空")
    return normalized


def _require_positive(value: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label}必须是正整数")
    return value


def _require_non_negative(value: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label}必须是非负整数")
    return value
