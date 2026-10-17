"""Agent 消息与工具调用的 DuckDB 仓储。

两处并发相关的设计：

1. **序号由编排层按会话快照分配**（`messages` 长度），数据库的
   ``UNIQUE (session_id, ordinal)`` 是兜底。这里**不**用 ``max(ordinal)+1`` 现算：
   并发两个回合会算出同一个值，然后其中一个失败——而失败点会晚到插入语句，
   错误信息只有约束名。
2. **``last_event_sequence`` 用比较并交换递增**。它是事件顺序的唯一依据；
   若用 ``+1`` 直接写，两个并发回合会产生相同序号，客户端看到的事件顺序就不确定。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import Any

import duckdb

from ...domain.agent.messages import AgentMessage, AgentToolCall


@dataclass(frozen=True)
class AgentMessageRepository:
    """消息与工具调用仓储。

    @param connection: 已迁移到当前 schema 的连接。
    """

    _connection: duckdb.DuckDBPyConnection

    def append_message(self, message: AgentMessage) -> None:
        """插入一条消息。

        @param message: 领域层消息。
        """
        self._connection.execute(
            """
            INSERT INTO agent_messages (
                id, session_id, ordinal, role, provider, status, content,
                error_code, created_at, completed_at
            ) VALUES (
                CAST(? AS UUID), CAST(? AS UUID), ?, ?, ?, ?, ?, ?,
                CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ)
            )
            """,
            [
                message.id, message.session_id, message.ordinal, message.role,
                message.provider, message.status, message.content, message.error_code,
                message.created_at, message.completed_at,
            ],
        )

    def complete_message(self, message: AgentMessage) -> bool:
        """把消息更新为终态。

        只更新仍处于 ``streaming`` 的行——否则一个迟到的完成回调会覆盖已终止消息的
        完成时间与错误码。

        @param message: 已终止的消息。
        @returns 是否更新成功。
        """
        row = self._connection.execute(
            """
            UPDATE agent_messages
            SET status = ?, content = ?, error_code = ?,
                completed_at = CAST(? AS TIMESTAMPTZ)
            WHERE id = CAST(? AS UUID) AND status = 'streaming'
            RETURNING id
            """,
            [
                message.status, message.content, message.error_code, message.completed_at,
                message.id,
            ],
        ).fetchone()
        return row is not None

    def load_messages(self, session_id: str) -> tuple[AgentMessage, ...]:
        """按序号升序读取会话的全部消息。"""
        rows = self._connection.execute(
            """
            SELECT id, session_id, ordinal, role, provider, status, content,
                   error_code, created_at, completed_at
            FROM agent_messages
            WHERE session_id = CAST(? AS UUID)
            ORDER BY ordinal
            """,
            [session_id],
        ).fetchall()
        return tuple(
            AgentMessage(
                id=str(row[0]), session_id=str(row[1]), ordinal=int(row[2]),
                role=row[3], provider=row[4], status=row[5], content=row[6],
                error_code=row[7], created_at=_iso(row[8]),
                completed_at=None if row[9] is None else _iso(row[9]),
            )
            for row in rows
        )

    def append_tool_call(self, call: AgentToolCall) -> None:
        """插入一次工具调用记录。"""
        self._connection.execute(
            """
            INSERT INTO agent_tool_calls (
                id, session_id, message_id, ordinal, tool_name, authorization_decision,
                status, arguments_summary, result_summary, ledger_version, duration_ms,
                error_code, started_at, completed_at
            ) VALUES (
                CAST(? AS UUID), CAST(? AS UUID), CAST(? AS UUID), ?, ?, ?, ?, ?, ?, ?, ?,
                ?, CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ)
            )
            """,
            [
                call.id, call.session_id, call.message_id, call.ordinal, call.tool_name,
                call.authorization_decision, call.status, _json(call.arguments_summary),
                None if call.result_summary is None else _json(call.result_summary),
                call.ledger_version, call.duration_ms, call.error_code,
                call.started_at, call.completed_at,
            ],
        )

    def complete_tool_call(self, call: AgentToolCall) -> bool:
        """把工具调用更新为终态；只更新仍处于 ``started`` 的行。"""
        row = self._connection.execute(
            """
            UPDATE agent_tool_calls
            SET status = ?, result_summary = ?, duration_ms = ?, error_code = ?,
                completed_at = CAST(? AS TIMESTAMPTZ)
            WHERE id = CAST(? AS UUID) AND status = 'started'
            RETURNING id
            """,
            [
                call.status,
                None if call.result_summary is None else _json(call.result_summary),
                call.duration_ms, call.error_code, call.completed_at, call.id,
            ],
        ).fetchone()
        return row is not None

    def load_tool_calls(self, session_id: str) -> tuple[dict[str, Any], ...]:
        """按序号读取会话的工具调用记录。

        **摘要原样返回**：它们已由领域层净化过，此处不再加工——加工会导致
        "库里存的"与"接口返回的"不一致，审计时对不上。
        """
        rows = self._connection.execute(
            """
            SELECT id, session_id, message_id, ordinal, tool_name, authorization_decision,
                   status, arguments_summary, result_summary, ledger_version, duration_ms,
                   error_code, started_at, completed_at
            FROM agent_tool_calls
            WHERE session_id = CAST(? AS UUID)
            ORDER BY ordinal
            """,
            [session_id],
        ).fetchall()
        return tuple(
            {
                "id": str(row[0]),
                "sessionId": str(row[1]),
                "messageId": str(row[2]),
                "ordinal": int(row[3]),
                "toolName": row[4],
                "authorizationDecision": row[5],
                "status": row[6],
                "argumentsSummary": _parse_json(row[7]),
                "resultSummary": None if row[8] is None else _parse_json(row[8]),
                "ledgerVersion": int(row[9]),
                "durationMs": None if row[10] is None else int(row[10]),
                "errorCode": row[11],
                "startedAt": _iso(row[12]),
                "completedAt": None if row[13] is None else _iso(row[13]),
            }
            for row in rows
        )

    def bump_event_sequence(
        self, session_id: str, expected: int, next_sequence: int
    ) -> bool:
        """以比较并交换推进会话事件序号。

        @param session_id: 会话 ID。
        @param expected: 调用方读到的事件序号。
        @param next_sequence: 目标序号。
        @returns 是否成功；``False`` 表示有并发回合先推进了。
        """
        row = self._connection.execute(
            """
            UPDATE agent_sessions
            SET last_event_sequence = ?, updated_at = current_timestamp
            WHERE id = CAST(? AS UUID) AND last_event_sequence = ?
            RETURNING id
            """,
            [next_sequence, session_id, expected],
        ).fetchone()
        return row is not None


def _parse_json(value: Any) -> Any:
    """把 DuckDB 返回的 JSON 列解析为对象。

    DuckDB 可能返回字符串或已解析的对象，两种都要接受。
    """
    import json

    if isinstance(value, str):
        return json.loads(value)
    return value


def _json(value: dict[str, Any]) -> str:
    """把摘要序列化为 JSON 文本。

    DuckDB 的 JSON 列接受字符串；``ensure_ascii=False`` 保留原始字符，
    便于人工核对摘要内容。
    """
    import json

    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _iso(value: Any) -> str:
    """把 DuckDB 返回的时间值渲染为与 JavaScript ``toISOString()`` 同形的文本。"""
    from datetime import datetime

    if isinstance(value, datetime):
        return (
            value.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return str(value)
