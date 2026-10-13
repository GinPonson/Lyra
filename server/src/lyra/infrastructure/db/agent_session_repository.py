"""Agent 会话的 DuckDB 仓储。

镜像 TypeScript 版 ``agent-session-repository.ts`` 的创建与读取部分。

**一处必须在文档里写明的语义分叉**：Agent 会话的 `accountScope.account` 存的是
**账户 UUID**（数据库列是 `account_id UUID REFERENCES accounts(id)`），而分析领域的
`AccountScope.account` 是**脱敏账户键**（`legacy_account_key`）。

两者共用同一个类型，因此极易混用。后果不是报错，而是**静默返回空账本**：
`WHERE legacy_account_key = '<uuid>'` 匹配不到任何行，分析结果为空但没有任何异常。

因此：**会话读写用 UUID；任何拿会话范围去读账本的地方，必须先把它解析为脱敏键。**
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import duckdb

from .account_lookup import resolve_legacy_account_key

from ...domain.agent.switching import (
    AgentProvider,
    AgentSessionSnapshot,
    AgentSessionStatus,
)
from ...domain.analytics.types import AccountScope


@dataclass(frozen=True)
class AgentSessionRow:
    """``agent_sessions`` 的完整行，供编排层使用。"""

    id: str
    device_id: str
    provider: AgentProvider
    # 迁移标记：非空表示该会话来自旧供应商（claude / pi / opencode）。
    original_provider: str | None
    status: AgentSessionStatus
    account_scope: AccountScope
    ledger_version: int
    as_of: str
    revision: int
    last_event_sequence: int
    summary: str | None
    created_at: str
    updated_at: str
    closed_at: str | None


class DuckDbAgentSessionRepository:
    """Agent 会话仓储。

    @param connection: 已迁移到当前 schema 的连接。
    """

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection

    def create_session(
        self,
        session: AgentSessionSnapshot,
        *,
        device_id: str,
        now: datetime,
    ) -> None:
        """插入一条新会话。

        同时写入 ``agent_session_identities``：该表与会话一一对应，用于在会话表被
        重建（迁移）时保留身份，因此**创建时就必须写**，事后无法补。

        @param session: 领域层快照（``account`` 是 UUID）。
        @param device_id: 发起设备。
        @param now: 服务器时间。
        """
        stamp = _iso(now)
        self._connection.execute(
            "INSERT INTO agent_session_identities (id) VALUES (CAST(? AS UUID))",
            [session.id],
        )
        self._connection.execute(
            """
            INSERT INTO agent_sessions (
                id, device_id, provider, original_provider, status, account_scope_kind,
                account_id, ledger_version, as_of, revision, last_event_sequence, summary,
                created_at, updated_at, closed_at
            ) VALUES (
                CAST(? AS UUID), CAST(? AS UUID), ?, ?, ?, ?, CAST(? AS UUID), ?,
                CAST(? AS TIMESTAMPTZ), ?, 0, ?, CAST(? AS TIMESTAMPTZ),
                CAST(? AS TIMESTAMPTZ), NULL
            )
            """,
            [
                # original_provider **不是**"原供应商"，而是迁移标记：只有从旧供应商
                # （claude / pi / opencode）迁移过来的会话才写值，新会话必须是 NULL。
                # 它的 CHECK 也只允许那三个旧值，写 "genkit" 会被数据库拒绝。
                session.id, device_id, session.provider, None, session.status,
                session.account_scope.kind,
                session.account_scope.account if session.account_scope.kind == "account" else None,
                session.ledger_version, session.as_of, session.revision, session.summary,
                stamp, stamp,
            ],
        )

    def load_session(self, session_id: str) -> AgentSessionRow | None:
        """按 ID 读取会话；不存在返回 ``None``。"""
        row = self._connection.execute(
            """
            SELECT id, device_id, provider, original_provider, status, account_scope_kind,
                   account_id, ledger_version, as_of, revision, last_event_sequence, summary,
                   created_at, updated_at, closed_at
            FROM agent_sessions WHERE id = CAST(? AS UUID)
            """,
            [session_id],
        ).fetchone()
        if row is None:
            return None
        return AgentSessionRow(
            id=str(row[0]),
            device_id=str(row[1]),
            provider=row[2],
            original_provider=row[3],
            status=row[4],
            account_scope=_parse_scope(row[5], row[6]),
            ledger_version=int(row[7]),
            as_of=_iso(row[8]),
            revision=int(row[9]),
            last_event_sequence=int(row[10]),
            summary=row[11],
            created_at=_iso(row[12]),
            updated_at=_iso(row[13]),
            closed_at=None if row[14] is None else _iso(row[14]),
        )

    def update_status(
        self,
        session_id: str,
        expected_status: AgentSessionStatus,
        next_status: AgentSessionStatus,
        now: datetime,
    ) -> bool:
        """以比较并交换更新会话状态。

        用 CAS 而不是无条件 UPDATE：两个并发的取消请求会让第二个把第一个的结果覆盖，
        而取消是**用户可见的状态**，不能出现"取消后又变回运行中"。

        @param session_id: 会话 ID。
        @param expected_status: 调用方读到的状态。
        @param next_status: 目标状态。
        @param now: 更新时间。
        @returns 是否成功。
        """
        # schema 的 CHECK 要求 closed ⟺ closed_at 非空，因此关闭时必须一并写入；
        # 其它状态保持 closed_at 为 NULL（终态无出边，无需清除路径）。
        stamp = _iso(now)
        row = self._connection.execute(
            """
            UPDATE agent_sessions
            SET status = ?, updated_at = CAST(? AS TIMESTAMPTZ),
                closed_at = CASE WHEN ? = 'closed' THEN CAST(? AS TIMESTAMPTZ) ELSE NULL END
            WHERE id = CAST(? AS UUID) AND status = ?
            RETURNING id
            """,
            [next_status, stamp, next_status, stamp, session_id, expected_status],
        ).fetchone()
        return row is not None

    def save_turn_state(
        self,
        session: AgentSessionSnapshot,
        *,
        expected_revision: int,
        next_event_sequence: int,
    ) -> bool:
        """在一次回合推进中保存会话状态与事件序号。

        以 ``revision`` 做比较并交换：两个并发回合读到同一个 revision 时只有一方能
        写入，另一方必须重读——否则两个回合会各自基于旧状态继续，最终把对方的消息
        覆盖掉。

        @param session: 新会话快照。
        @param expected_revision: 调用方读到的 revision。
        @param next_event_sequence: 已产生的事件序号。
        @returns 是否成功。
        """
        row = self._connection.execute(
            """
            UPDATE agent_sessions
            SET status = ?, revision = ?, last_event_sequence = ?,
                updated_at = current_timestamp
            WHERE id = CAST(? AS UUID) AND revision = ?
            RETURNING id
            """,
            [session.status, session.revision, next_event_sequence,
             session.id, expected_revision],
        ).fetchone()
        return row is not None

    def resolve_legacy_account_key(self, account_id: str) -> str | None:
        """把会话里的账户 UUID 解析为账本用的脱敏账户键。

        委托给共享实现（:func:`account_lookup.resolve_legacy_account_key`）——
        **不复制那份查询**，否则两处对"账户不存在"的处理迟早会分叉。

        @param account_id: 账户 UUID。
        @returns 脱敏账户键；账户不存在时 ``None``。
        """
        return resolve_legacy_account_key(self._connection, account_id)


def _parse_scope(kind: Any, account_id: Any) -> AccountScope:
    """还原账户范围。

    **``kind == "account"`` 时 ``account`` 是 UUID**，不是脱敏键。

    @raises ValueError: 存储值与 schema 约束不一致。
    """
    if kind == "all" and account_id is None:
        return AccountScope(kind="all")
    # DuckDB 的 UUID 列返回 ``uuid.UUID`` 对象而不是字符串，因此统一按字符串处理。
    if kind == "account" and account_id is not None and str(account_id):
        return AccountScope(kind="account", account=str(account_id))
    raise ValueError("Agent 会话账户范围与数据库 schema 不一致")


def _iso(value: Any) -> str:
    """把 DuckDB 返回的时间值渲染为与 JavaScript ``toISOString()`` 同形的文本。"""
    if isinstance(value, datetime):
        return (
            value.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return str(value)
