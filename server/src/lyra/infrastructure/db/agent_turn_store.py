"""回合编排器的 DuckDB 仓储适配器。

把编排器需要的操作（保存会话状态、原子地起一个回合、追加终态消息、读取会话状态）
映射到具体表。**适配器很薄是刻意的**：编排逻辑不应知道表结构，否则领域测试就得建
真实数据库。

**这里同时是"领域对象 → 仓储对象"的唯一转换点。** 领域层的
``NormalizedAgentMessage`` 只描述消息内容，而 ``agent_messages`` 需要会话归属、序号、
供应商与完成时间。之前把领域对象直接交给消息仓储，导致每个真实回合在第一次落库时
抛 ``AttributeError``——而领域测试用内存假仓储，所以没被发现。
"""

from __future__ import annotations

import duckdb

from ...domain.agent.messages import AgentMessage, AgentToolCall, TurnMessage
from ...domain.agent.switching import AgentSessionSnapshot
from .agent_message_repository import AgentMessageRepository
from .agent_session_repository import DuckDbAgentSessionRepository


class DuckDbAgentTurnStore:
    """实现 ``AgentTurnStore`` 协议。

    @param connection: 已迁移到当前 schema 的连接。
    """

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection
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

    def save_turn_start(
        self,
        session: AgentSessionSnapshot,
        message: TurnMessage,
        *,
        next_event_sequence: int,
    ) -> bool:
        """在一个事务里写入 running 快照与用户消息。

        **两步必须在同一事务**：分散写时后一步失败会留下"会话已 running、没有用户
        消息"的半写状态，而 ``send_message`` 只在 ``idle`` 受理、``DELETE`` 不允许
        ``running→closed``——会话会永久卡死，只能手工改库。

        @returns 会话 CAS 是否成功；失败时**不留下任何一行**。
        """
        self._connection.execute("BEGIN TRANSACTION")
        try:
            if not self.save_session(session, next_event_sequence=next_event_sequence):
                self._connection.execute("ROLLBACK")
                return False
            self._messages.append_message(_row(message))
            self._connection.execute("COMMIT")
        except Exception as error:
            try:
                self._connection.execute("ROLLBACK")
            except Exception as rollback_error:  # noqa: BLE001 - 回滚失败必须一并暴露
                raise RuntimeError(
                    f"回合起始写入失败且回滚失败: {error!r} / 回滚错误: {rollback_error!r}"
                ) from error
            raise
        return True

    def append_turn_message(self, message: TurnMessage) -> None:
        """追加一条终态消息。"""
        self._messages.append_message(_row(message))

    def append_tool_call(self, call: AgentToolCall) -> None:
        """插入一条工具调用审计行。"""
        self._messages.append_tool_call(call)

    def complete_tool_call(self, call: AgentToolCall) -> bool:
        """把工具调用更新为终态；只更新仍处于 ``started`` 的行。"""
        return self._messages.complete_tool_call(call)

    def session_status(self, session_id: str) -> str | None:
        """读取会话当前状态；不存在返回 ``None``。

        编排器据此判断"用户是否已请求取消"——**读持久化状态而不是内存标志**，因为
        取消请求可能来自另一个线程，且必须活过进程重启。
        """
        row = self._sessions.load_session(session_id)
        return None if row is None else row.status


def _row(message: TurnMessage) -> AgentMessage:
    """把编排器的回合消息映射为仓储行对象。"""
    return AgentMessage(
        id=message.id,
        session_id=message.session_id,
        ordinal=message.ordinal,
        role=message.role,  # type: ignore[arg-type]
        provider=message.provider,  # type: ignore[arg-type]
        status=message.status,
        content=message.content,
        error_code=message.error_code,
        created_at=message.created_at,
        completed_at=message.completed_at,
    )
