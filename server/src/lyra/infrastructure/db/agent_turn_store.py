"""回合编排器的 DuckDB 仓储适配器。

把编排器需要的两个操作（保存会话状态、追加消息）映射到具体表。**适配器很薄是
刻意的**：编排逻辑不应知道表结构，否则领域测试就得建真实数据库。
"""

from __future__ import annotations

import duckdb

from ...domain.agent.switching import AgentSessionSnapshot
from .agent_message_repository import AgentMessageRepository
from .agent_session_repository import DuckDbAgentSessionRepository


class DuckDbAgentTurnStore:
    """实现 ``AgentTurnStore`` 协议。

    @param connection: 已迁移到当前 schema 的连接。
    """

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._sessions = DuckDbAgentSessionRepository(connection)
        self._messages = AgentMessageRepository(connection)

    def save_session(
        self, session: AgentSessionSnapshot, *, next_event_sequence: int
    ) -> bool:
        """以 revision 做 CAS 保存会话状态与事件序号。

        传入的 ``session.revision`` 是**新** revision，因此期望值取 ``revision - 1``。
        """
        return self._sessions.save_turn_state(
            session,
            expected_revision=session.revision - 1,
            next_event_sequence=next_event_sequence,
        )

    def append_message(self, message) -> None:
        """追加一条消息。"""
        self._messages.append_message(message)
