"""Agent 可用性快照。

镜像 TypeScript 版 ``agent-availability.ts``。

两个要点：

1. **不可用时给出稳定原因码**（``not_configured``），而不是自由文本。客户端据此
   决定是提示"服务端未配置"还是"服务暂时不可用"，而自由文本无法稳定分支；
2. **``checkedAt`` 由调用方传入**而不是内部取当前时间——同一份快照被多次引用时，
   时间必须是同一个值，否则客户端会看到自相矛盾的状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class AgentAvailabilitySnapshot:
    """Agent 可用性快照。"""

    checked_at: str
    providers: tuple[dict[str, Any], ...]


def create_agent_availability_snapshot(
    *,
    configured: bool,
    checked_at: datetime,
) -> AgentAvailabilitySnapshot:
    """按配置状态生成快照。

    @param configured: 模型提供方是否已完整配置。
    @param checked_at: 服务端可信的检查时间。
    @returns 可用性快照。
    @raises ValueError: 时间不带时区。
    """
    if not isinstance(checked_at, datetime) or checked_at.tzinfo is None:
        raise ValueError("Agent 可用性检查时间必须带时区")
    provider: dict[str, Any] = (
        {"provider": "genkit", "name": "Lyra", "status": "available", "version": "Genkit"}
        if configured
        else {
            "provider": "genkit",
            "name": "Lyra",
            "status": "unavailable",
            "reason": "not_configured",
        }
    )
    return AgentAvailabilitySnapshot(
        checked_at=(
            checked_at.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        ),
        providers=(provider,),
    )
