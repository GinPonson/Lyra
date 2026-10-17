"""Agent 会话的 DuckDB 仓储。

镜像 TypeScript 版 ``agent-session-repository.ts`` 的创建与读取部分。

**一处必须在文档里写明的语义分叉**：Agent 会话的 `accountScope.account` 存的是
**账户 UUID**（数据库列是 `account_id UUID REFERENCES accounts(id)`），而分析领域的
`AccountScope.account` 是**脱敏账户键**（`legacy_account_key`）。

两者共用同一个类型，因此极易混用。后果不是报错，而是**静默返回空账本**：
`WHERE legacy_account_key = '<uuid>'` 匹配不到任何行，分析结果为空但没有任何异常。

因此：**会话读写用 UUID；任何拿会话范围去读账本的地方，必须先把它解析为脱敏键。**

**归属一律经不可变会话身份取得**：``agent_sessions`` 自己不带 ``user_id``（它是可变表，
状态会被反复改写），用户归属记在一一对应的 ``agent_session_identities`` 上。因此本模块
的每一次按 ID 读取都从那张表 JOIN 出 ``user_id``；调用方拿到的行自带归属，越权读取在
``load_session_for_user`` 处收敛为 ``None``（与"不存在"不可区分，计划 §4.2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import duckdb

from .account_lookup import resolve_legacy_account_key

from ...application.app_error import ApplicationError
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
    #: 会话所属用户（来自不可变的 ``agent_session_identities``）；归属判定的唯一依据。
    user_id: str
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
        user_id: str,
        now: datetime,
    ) -> None:
        """插入一条新会话。

        同时写入 ``agent_session_identities``：该表与会话一一对应，用于在会话表被
        重建（迁移）时保留身份，因此**创建时就必须写**，事后无法补。用户归属也只在
        那一处记录。

        **创建设备与绑定账户必须同属该用户**：``agent_sessions`` 对二者的外键都是单列
        的，数据库只能保证"这个设备/账户存在"，不保证"属于同一个用户"（P0 契约 §3.2）。
        因此这里显式校验一次——否则一次拼错的调用就能把 B 的账户挂到 A 的会话上，
        而后续所有工具调用都会按会话归属去读那个账户。

        @param session: 领域层快照（``account`` 是 UUID）。
        @param device_id: 发起设备；必须属于 ``user_id``。
        @param user_id: 会话所属用户 UUID 文本；写入不可变会话身份。
        @param now: 服务器时间。
        @raises ApplicationError: 设备或账户不属于该用户（400）。
        """
        self._require_owner("paired_devices", device_id, user_id, label="设备")
        if session.account_scope.kind == "account":
            self._require_owner(
                "accounts", session.account_scope.account or "", user_id, label="账户"
            )
        stamp = _iso(now)
        self._connection.execute(
            "INSERT INTO agent_session_identities (id, user_id) VALUES (CAST(? AS UUID), CAST(? AS UUID))",
            [session.id, user_id],
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
        """按 ID 读取会话；不存在返回 ``None``。

        **不带用户范围，仅供回合编排器内部使用**（它读的是自己已经登记、且登记前
        已按用户校验过归属的会话状态）。任何 HTTP/WS 入口都必须改用
        :meth:`load_session_for_user`——返回的行虽然带 ``user_id``，但"取到行再比对"
        这种写法漏一次比对就是一次越权，不如让查询本身带范围。
        """
        row = self._connection.execute(
            f"""
            SELECT {_SESSION_COLUMNS}
            FROM agent_sessions s
            JOIN agent_session_identities i ON i.id = s.id
            WHERE s.id = CAST(? AS UUID)
            """,
            [session_id],
        ).fetchone()
        return None if row is None else _row(row)

    def load_session_for_user(self, session_id: str, user_id: str) -> AgentSessionRow | None:
        """按「用户 + 会话 ID」读取会话。

        不属于该用户的会话与不存在的会话**返回同一个 ``None``**：调用方一律回 404，
        让"有效资源但不属于你"与"资源不存在"不可区分，避免用错误码探测他人资源
        （计划 §4.2）。

        @param session_id: 会话 ID。
        @param user_id: 当前用户 UUID 文本。
        @returns 会话行；不存在或不属于该用户时为 ``None``。
        """
        row = self._connection.execute(
            f"""
            SELECT {_SESSION_COLUMNS}
            FROM agent_sessions s
            JOIN agent_session_identities i ON i.id = s.id
            WHERE s.id = CAST(? AS UUID) AND i.user_id = CAST(? AS UUID)
            """,
            [session_id, user_id],
        ).fetchone()
        return None if row is None else _row(row)

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

    def resolve_legacy_account_key(self, user_id: str, account_id: str) -> str | None:
        """把会话里的账户 UUID 解析为账本用的脱敏账户键。

        委托给共享实现（:func:`account_lookup.resolve_legacy_account_key`）——
        **不复制那份查询**，否则两处对"账户不存在"的处理迟早会分叉。``user_id`` 必须
        一起转发：只按 ``account_id`` 全库解析会让 A 传 B 的账户 UUID 时拿到 B 的脱敏
        键，随后账本查询就带上了别人的账户标识（P0 契约 §3.11）。

        @param user_id: 当前用户 UUID 文本。
        @param account_id: 账户 UUID。
        @returns 脱敏账户键；账户不存在或不属于该用户时 ``None``。
        """
        return resolve_legacy_account_key(self._connection, user_id, account_id)

    def _require_owner(self, table: str, resource_id: str, user_id: str, *, label: str) -> None:
        """确认某个资源存在且属于该用户，否则明确失败。

        ``table`` 只接受本模块内固定的两个表名（不是用户输入），因此可以安全地拼进
        SQL；资源 ID 与用户 ID 仍走参数绑定。

        @param table: ``paired_devices`` 或 ``accounts``。
        @param resource_id: 资源 UUID 文本。
        @param user_id: 期望的归属用户。
        @param label: 错误信息里的资源名。
        @raises ApplicationError: 资源不存在或不属于该用户（400）。
        """
        if table not in _OWNED_TABLES:
            raise ValueError(f"不支持的归属校验表: {table}")
        found = self._connection.execute(
            f"SELECT 1 FROM {table} WHERE id = CAST(? AS UUID) AND user_id = CAST(? AS UUID)",
            [resource_id, user_id],
        ).fetchone()
        if found is None:
            raise ApplicationError(
                code="AGENT_SESSION_SCOPE_INVALID",
                message=f"会话{label}不属于当前用户",
                status_code=400,
                retryable=False,
            )


#: 允许做归属校验的白名单表；拼进 SQL 的表名只能取自这里。
_OWNED_TABLES = frozenset({"paired_devices", "accounts"})

#: 会话行读取的列顺序——``load_session`` 与 ``load_session_for_user`` 共用，
#: 保证两者还原出的行形状完全一致（否则会出现"按用户读到的行少一个字段"这种偏差）。
_SESSION_COLUMNS = (
    "s.id, s.device_id, i.user_id, s.provider, s.original_provider, s.status, "
    "s.account_scope_kind, s.account_id, s.ledger_version, s.as_of, s.revision, "
    "s.last_event_sequence, s.summary, s.created_at, s.updated_at, s.closed_at"
)


def _row(row: Any) -> AgentSessionRow:
    """把查询结果还原为会话行对象。"""
    return AgentSessionRow(
        id=str(row[0]),
        device_id=str(row[1]),
        user_id=str(row[2]),
        provider=row[3],
        original_provider=row[4],
        status=row[5],
        account_scope=_parse_scope(row[6], row[7]),
        ledger_version=int(row[8]),
        as_of=_iso(row[9]),
        revision=int(row[10]),
        last_event_sequence=int(row[11]),
        summary=row[12],
        created_at=_iso(row[13]),
        updated_at=_iso(row[14]),
        closed_at=None if row[15] is None else _iso(row[15]),
    )


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
