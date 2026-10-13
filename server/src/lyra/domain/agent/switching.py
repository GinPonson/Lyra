"""Agent 会话的状态与回合边界切换。

镜像 TypeScript 版 ``domain/agent/switching.ts``。

**账户范围与数据基线只能在完整回合边界变更**（``idle``）。理由不是实现便利：
半途换范围会让同一段对话里前后数字口径不同，而模型已经引用过旧数字。

两处切换的共同性质：**旧摘要必须作废**。

摘要是已完成回合的压缩，其中可能含原账户或旧基线的具体数字。若保留它，下一回合
模型会拿旧摘要里的数字回答新范围的问题——**而且答案看起来完全正常**。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from ...domain.analytics.types import AccountScope

AgentProvider = Literal["genkit"]
AgentSessionStatus = Literal["idle", "running", "cancelling", "closed"]
AgentSwitchErrorCode = Literal["AGENT_SWITCH_REQUIRES_TURN_BOUNDARY", "AGENT_UNAVAILABLE"]

AgentMessageRole = Literal["user", "assistant", "tool", "system"]
AgentMessageStatus = Literal["completed", "failed", "cancelled"]


class AgentSwitchError(Exception):
    """回合边界或供应商状态阻止了切换。

    @param code: 客户端可稳定分支的错误码。
    @param message: 不含命令、路径或凭据的安全说明。
    @param internal_reason: **仅供服务端日志**的探测原因，传输层不得返回。
    """

    def __init__(
        self, code: AgentSwitchErrorCode, message: str, internal_reason: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.internal_reason = internal_reason


@dataclass(frozen=True)
class AgentProviderAvailability:
    """供应商探测后的可用状态；不可用必须保留明确原因。"""

    provider: AgentProvider
    status: Literal["available", "unavailable"]
    reason: str | None = None


class _Unset:
    """区别于 ``None`` 的「未设置」标记。

    TypeScript 用 ``undefined`` 与 ``null`` 区分「字段不存在」与「字段存在但为空」，
    且公开投影会保留后者（``{errorCode: null}``）。Python 的 ``None`` 只能表达一种，
    因此需要哨兵。
    """

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return "UNSET"


UNSET = _Unset()


@dataclass(frozen=True)
class NormalizedAgentMessage:
    """可迁移的规范化消息，不含模型隐藏推理或供应商内部状态。"""

    id: str
    role: AgentMessageRole
    content: str
    created_at: str
    # 助手消息的持久终态；用户与系统消息不设置（``None`` 表示未设置）。
    status: AgentMessageStatus | None = None
    # ``UNSET`` 表示未设置；``None`` 表示**显式为空**——两者在公开响应里不同。
    error_code: str | None | _Unset = UNSET


@dataclass(frozen=True)
class AgentSessionSnapshot:
    """持久化的 Agent 会话快照。"""

    id: str
    provider: AgentProvider
    status: AgentSessionStatus
    account_scope: AccountScope
    ledger_version: int
    as_of: str
    revision: int
    # 已完成回合的规范化摘要；不含隐藏推理或供应商缓存。
    summary: str | None
    messages: tuple[NormalizedAgentMessage, ...]


@dataclass(frozen=True)
class AgentContextChangePlan:
    """一次上下文变更的计划：新快照与划界用的系统消息。"""

    next_session: AgentSessionSnapshot
    context_message: NormalizedAgentMessage


def create_agent_session(
    *,
    session_id: str,
    provider: AgentProvider,
    availability: AgentProviderAvailability,
    account_scope: AccountScope,
    ledger_version: int,
    as_of: str,
) -> AgentSessionSnapshot:
    """创建绑定首个数据基线的空会话。

    创建**不启动模型回合**；调用方先持久化快照。模型未配置时明确失败。

    @param session_id: 会话 ID。
    @param provider: 会话使用的供应商。
    @param availability: 该供应商的可用性。
    @param account_scope: 初始账户范围。
    @param ledger_version: 初始账本版本，必须是非负整数。
    @param as_of: 初始数据截止时间。
    @returns 新的空会话快照。
    @raises ValueError: 参数非法。
    @raises AgentSwitchError: 供应商不可用（``AGENT_UNAVAILABLE``）。
    """
    resolved_id = _require_text(session_id, "Agent 会话 ID")
    if availability.provider != provider:
        raise ValueError("Agent 可用性结果与会话供应商不一致")
    if availability.status == "unavailable":
        raise AgentSwitchError(
            "AGENT_UNAVAILABLE",
            f"Agent {provider} 当前不可用",
            availability.reason,
        )
    return AgentSessionSnapshot(
        id=resolved_id,
        provider=provider,
        status="idle",
        account_scope=_normalize_scope(account_scope),
        ledger_version=_require_version(ledger_version, "Agent 初始账本版本"),
        as_of=_require_text(as_of, "Agent 初始数据截止时间"),
        revision=1,
        summary=None,
        messages=(),
    )


def prepare_agent_account_scope_change(
    *,
    session: AgentSessionSnapshot,
    account_scope: AccountScope,
    ledger_version: int,
    as_of: str,
    message_id: str,
    changed_at: str,
) -> AgentContextChangePlan:
    """在完整回合边界切换账户范围与账本基线。

    旧消息保留用于对话连续性，但新增 system 消息划清前后口径；**旧摘要必须失效**——
    它可能含原账户的数字。

    @param session: 当前会话。
    @param account_scope: 目标账户范围。
    @param ledger_version: 新账本版本。
    @param as_of: 新数据截止时间。
    @param message_id: 划界系统消息的 ID。
    @param changed_at: 变更时间。
    @returns 新快照与划界消息。
    @raises AgentSwitchError: 不在回合边界（``AGENT_SWITCH_REQUIRES_TURN_BOUNDARY``）。
    @raises ValueError: 范围未变、版本非法或消息 ID 重复。
    """
    _require_turn_boundary(session, "账户范围")
    next_scope = _normalize_scope(account_scope)
    if _same_scope(session.account_scope, next_scope):
        raise ValueError("Agent 会话已经使用目标账户范围")
    version = _require_version(ledger_version, "Agent 新账本版本")
    resolved_as_of = _require_text(as_of, "Agent 新数据截止时间")
    resolved_message_id = _require_message_id(session, message_id, "Agent 账户范围变更消息 ID")
    resolved_changed_at = _require_text(changed_at, "Agent 账户范围变更时间")

    context_message = NormalizedAgentMessage(
        id=resolved_message_id,
        role="system",
        content=(
            f"账户范围已从{_format_scope(session.account_scope)}"
            f"切换为{_format_scope(next_scope)}；"
            f"后续回答使用账本版本 {version}，截止时间 {resolved_as_of}。"
            "此前回答仍按其原始账户范围和数据基线理解。"
        ),
        created_at=resolved_changed_at,
    )
    return AgentContextChangePlan(
        next_session=replace(
            session,
            account_scope=next_scope,
            ledger_version=version,
            as_of=resolved_as_of,
            revision=session.revision + 1,
            # 旧摘要可能含原账户数字，必须清除。
            summary=None,
            messages=(*session.messages, context_message),
        ),
        context_message=context_message,
    )


def prepare_agent_ledger_baseline_refresh(
    *,
    session: AgentSessionSnapshot,
    ledger_version: int,
    as_of: str,
    message_id: str,
    refreshed_at: str,
) -> AgentContextChangePlan:
    """在账户范围不变时，把已完成的数据更新显式带入会话。

    只接受**严格递增**的账本版本；旧摘要可能含已过期数字，必须清除。

    @param session: 当前会话。
    @param ledger_version: 新账本版本，必须大于当前值。
    @param as_of: 新数据截止时间。
    @param message_id: 划界系统消息的 ID。
    @param refreshed_at: 刷新时间。
    @returns 新快照与划界消息。
    @raises AgentSwitchError: 不在回合边界。
    @raises ValueError: 版本未递增、非法或消息 ID 重复。
    """
    _require_turn_boundary(session, "数据基线")
    version = _require_version(ledger_version, "Agent 新账本版本")
    if version <= session.ledger_version:
        raise ValueError("Agent 数据基线只能刷新到更高的账本版本")
    resolved_as_of = _require_text(as_of, "Agent 新数据截止时间")
    resolved_message_id = _require_message_id(session, message_id, "Agent 数据基线刷新消息 ID")
    resolved_refreshed_at = _require_text(refreshed_at, "Agent 数据基线刷新时间")

    context_message = NormalizedAgentMessage(
        id=resolved_message_id,
        role="system",
        content=(
            f"账户范围保持为{_format_scope(session.account_scope)}；"
            f"账本数据已从版本 {session.ledger_version} 更新到版本 {version}，"
            f"截止时间 {resolved_as_of}。"
            "后续回答使用新基线，此前回答仍按原始数据基线理解。"
        ),
        created_at=resolved_refreshed_at,
    )
    return AgentContextChangePlan(
        next_session=replace(
            session,
            ledger_version=version,
            as_of=resolved_as_of,
            revision=session.revision + 1,
            summary=None,
            messages=(*session.messages, context_message),
        ),
        context_message=context_message,
    )


def _require_turn_boundary(session: AgentSessionSnapshot, label: str) -> None:
    """确认会话处于完整回合边界（``idle``）。

    @raises AgentSwitchError: 状态不是 ``idle``。
    """
    if session.status != "idle":
        raise AgentSwitchError(
            "AGENT_SWITCH_REQUIRES_TURN_BOUNDARY",
            f"Agent {label}只能在完整回合边界切换，当前状态为 {session.status}",
        )


def _normalize_scope(scope: AccountScope) -> AccountScope:
    """复制并校验模型不可控制的目标账户范围。"""
    if scope.kind == "all":
        return AccountScope(kind="all")
    return AccountScope(kind="account", account=_require_text(scope.account, "Agent 目标账户"))


def _same_scope(left: AccountScope, right: AccountScope) -> bool:
    """判断两个显式账户范围是否相同。"""
    return left.kind == right.kind and (
        left.kind == "all" or (right.kind == "account" and left.account == right.account)
    )


def _format_scope(scope: AccountScope) -> str:
    """生成人类可复核、不省略范围类型的上下文标签。"""
    return "全部活跃账户" if scope.kind == "all" else f"单账户 {scope.account}"


def _require_version(value: object, label: str) -> int:
    """要求非负整数版本号。"""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label}必须是非负安全整数")
    return value


def _require_message_id(
    session: AgentSessionSnapshot, value: str, label: str
) -> str:
    """要求消息 ID 非空且未在会话中出现。"""
    normalized = _require_text(value, label)
    if any(message.id == normalized for message in session.messages):
        raise ValueError(f"{label}已存在")
    return normalized


def _require_text(value: str, label: str) -> str:
    """规范文本字段并拒绝空字符串。"""
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{label}不能为空")
    return normalized
