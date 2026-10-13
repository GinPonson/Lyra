"""Agent 工具调用的摘要净化与授权判定。

镜像 TypeScript 版 ``domain/agent/tool-call.ts`` 的安全部分。

**为什么必须净化摘要**：摘要会写入数据库并被回放给用户。若把原始参数或第三方响应
直接存进去，access token、OAuth code、邮箱、邮件正文就永久留在库里——
**而且它们看起来只是"一次调用的参数"**。

因此采用**拒绝而非过滤**：命中敏感字段名时直接抛错，而不是静默删掉该字段。
静默过滤会让调用方以为摘要完整入库，实际丢了字段，事后无从发现。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from ..analytics.types import AccountScope

AgentToolSummary = dict[str, str | int | float | bool | None]
AgentToolRejectionReason = Literal[
    "unknown_tool", "unconfigured_tool", "invalid_arguments", "forbidden_capability"
]

# 摘要字段数上限：摘要用于审计与回放，不是数据通道。
MAX_SUMMARY_FIELDS = 32
MAX_KEY_LENGTH = 64
MAX_TEXT_LENGTH = 512

# 明确禁止进入参数或结果摘要的敏感字段（键名已归一：小写、去非字母数字）。
FORBIDDEN_SUMMARY_KEYS = frozenset({
    "authorization",
    "cookie",
    "oauthcode",
    "accesstoken",
    "refreshtoken",
    "token",
    "email",
    "accountnumber",
    "mailbody",
    "body",
    "prompt",
    "hiddenreasoning",
    "privatekey",
})

_NON_ALNUM = re.compile(r"[^a-z0-9]")


class AgentToolPolicyError(Exception):
    """工具调用违反安全策略。"""

    code = "AGENT_TOOL_FORBIDDEN"


@dataclass(frozen=True)
class AgentToolContext:
    """服务端为一次 Agent 工具调用注入的不可变口径。

    **它不是模型可控参数。** 账户范围、账本版本与截止时间只来自会话快照；把它们
    放进工具的公开 schema 就等于让模型自己声明授权范围——会话绑定单账户时，一次
    不传范围字段的调用会静默升级为"全部账户"。

    @param session_id: 所属会话。
    @param device_id: 发起会话的设备；工具调用因此可归属到一次可撤销的登录。
    @param user_message_id: 工具调用挂靠的**已落库**消息 ID。``agent_tool_calls``
        对 ``agent_messages`` 有外键，而助手消息要到回合结束才落库，所以工具调用
        只能绑定本轮的用户消息。
    @param account_scope: 会话授权范围。``account`` 是账户 **UUID**——不是账本用的
        脱敏账户键，两者类型相同，混用不会报错、只会返回空账本。
    @param ledger_version: 权威数据基线版本。
    @param as_of: 数据截止时间（会话存储的 ISO 时间戳）。
    """

    session_id: str
    device_id: str
    user_message_id: str
    account_scope: AccountScope
    ledger_version: int
    as_of: str


def normalize_summary(summary: dict[str, Any], label: str) -> AgentToolSummary:
    """校验并复制一份扁平、有界的摘要。

    四条边界，缺任何一条都会让原始响应或敏感字段漏进数据库：

    - 字段数 ≤ 32、字段名长度 1..64；
    - 字段名归一后不得命中敏感集合；
    - 数值必须有限（拒绝 ``NaN`` / ``Infinity``）；
    - 文本 ≤ 512 字符，且值只能是标量（**不允许嵌套对象**）。

    @param summary: 待净化的摘要。
    @param label: 用于错误信息的标签（"参数" 或 "结果"）。
    @returns 归一化后的摘要副本。
    @raises AgentToolPolicyError: 命中敏感字段名。
    @raises ValueError: 违反其它边界。
    """
    if len(summary) > MAX_SUMMARY_FIELDS:
        raise ValueError(f"Agent 工具{label}摘要最多包含 {MAX_SUMMARY_FIELDS} 个字段")
    normalized: AgentToolSummary = {}
    for key, value in summary.items():
        if not isinstance(key, str) or not 1 <= len(key) <= MAX_KEY_LENGTH:
            raise ValueError(f"Agent 工具{label}摘要字段名长度无效")
        if _NON_ALNUM.sub("", key.lower()) in FORBIDDEN_SUMMARY_KEYS:
            # 拒绝而非过滤：静默删字段会让调用方以为摘要完整入库。
            raise AgentToolPolicyError(f"Agent 工具{label}摘要禁止包含敏感字段: {key}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"Agent 工具{label}摘要必须使用有限数值: {key}")
        if isinstance(value, str) and len(value) > MAX_TEXT_LENGTH:
            raise ValueError(f"Agent 工具{label}摘要文本过长: {key}")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"Agent 工具{label}摘要必须是扁平标量: {key}")
        normalized[key] = value
    return normalized


def authorize_tool_call(
    *,
    tool_name: str,
    known_tools: frozenset[str],
    configured_tools: frozenset[str],
) -> AgentToolRejectionReason | None:
    """判定一次工具调用是否放行。

    @param tool_name: 模型请求的工具名。
    @param known_tools: 目录中已知的工具名。
    @param configured_tools: 当前实际启用的工具名。
    @returns ``None`` 表示放行；否则给出拒绝原因。
    """
    if tool_name not in known_tools:
        return "unknown_tool"
    if tool_name not in configured_tools:
        return "unconfigured_tool"
    return None
