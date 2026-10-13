"""Agent 会话的状态转换。

镜像 TypeScript 版 ``domain/agent/lifecycle.ts``。

```
idle       → running | closed
running    → cancelling | idle
cancelling → idle
closed     → （无出边）
```

两条性质：

1. **同状态重放幂等且返回原对象**，不递增 `revision`。`revision` 是客户端判断
   "会话是否变过"的依据；一次无变化的轮询若也递增它，客户端会以为状态变了。
2. **`running` 不能直接回到 `idle` 之外的终态**——必须先经 `cancelling`。这保证
   取消有一个可观测的中间态，而不是从"正在生成"直接跳到"已取消"。

调用方必须先把供应商终态事件与消息固化，再把 `running` / `cancelling` 转回 `idle`：
状态先回 `idle` 会让客户端以为可以立刻发下一轮，而此时上一轮的消息还没落库。
"""

from __future__ import annotations

from dataclasses import replace

from .switching import AgentSessionSnapshot, AgentSessionStatus

# 允许的显式状态边。
ALLOWED_TRANSITIONS: dict[AgentSessionStatus, tuple[AgentSessionStatus, ...]] = {
    "idle": ("running", "closed"),
    "running": ("cancelling", "idle"),
    "cancelling": ("idle",),
    "closed": (),
}


class AgentSessionStateError(Exception):
    """状态转换被当前回合边界拒绝。"""

    code = "AGENT_SESSION_STATE_CONFLICT"

    def __init__(self, from_status: AgentSessionStatus, to_status: AgentSessionStatus) -> None:
        super().__init__(f"Agent 会话不能从 {from_status} 转换为 {to_status}")
        self.from_status = from_status
        self.to_status = to_status


def transition_agent_session(
    session: AgentSessionSnapshot, target: AgentSessionStatus
) -> AgentSessionSnapshot:
    """生成会话的下一持久化快照。

    @param session: 当前不可变快照。
    @param target: 编排器请求的新状态。
    @returns revision 已递增的新快照；目标与当前相同时返回**原对象**。
    @raises AgentSessionStateError: 转换不在允许的边内。
    @raises ValueError: 目标状态不是已知状态。
    """
    if target not in ALLOWED_TRANSITIONS:
        raise ValueError(f"未知的 Agent 会话状态: {target}")
    if session.status == target:
        # 幂等重放：返回原对象，调用方据此跳过写库与 revision 递增。
        return session
    if target not in ALLOWED_TRANSITIONS[session.status]:
        raise AgentSessionStateError(session.status, target)
    return replace(session, status=target, revision=session.revision + 1)
