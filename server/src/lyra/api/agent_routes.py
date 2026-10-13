"""Agent 路由。

| 路由 | 状态 |
| --- | --- |
| ``GET /v1/agents`` | ✅ 能力发现 |
| ``POST /v1/agent/sessions`` | ✅ 创建会话 |
| ``GET /v1/agent/sessions/:id`` | ✅ 读取会话 |
| ``GET /v1/agent/sessions/:id/tool-calls`` | ✅ 审计列表 |
| ``POST /v1/agent/sessions/:id/cancel`` | ✅ 请求取消 |
| ``DELETE /v1/agent/sessions/:id`` | ✅ 关闭 |
| ``POST /v1/agent/sessions/:id/messages`` | ✅ 执行一个回合（需模型已配置） |

``GET /v1/agents`` 的用途是让 App 判断"服务端是否配好了模型、能否进入对话界面"，
因此它**不返回工具清单**（工具属于会话内的能力，与可用性无关）。

**取消是两阶段的**：``POST .../cancel`` 只把状态推进到 ``cancelling``，真正的中止
由编排器在收到供应商信号后完成并回到 ``idle``。客户端看到 ``cancelling`` 表示
"请求已受理、尚未停止"。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..application.agent_availability import create_agent_availability_snapshot
from ..application.public_projection import project_agent_session
from ..domain.agent.lifecycle import AgentSessionStateError, transition_agent_session
from ..domain.agent.switching import (
    AgentProviderAvailability,
    AgentSessionSnapshot,
    create_agent_session,
)
from ..domain.analytics.types import AccountScope
from ..infrastructure.db.agent_message_repository import AgentMessageRepository
from ..infrastructure.db.agent_session_repository import DuckDbAgentSessionRepository
from .dependencies import _UUID_PATTERN, require_device

# 单条消息上限，与参考实现的 schema 一致（8192 字符）。
_MAX_MESSAGE_LENGTH = 8_192


def create_agent_router() -> APIRouter:
    """构造 Agent 路由。

    @returns 能力发现端点；要求设备认证。
    """
    router = APIRouter(dependencies=[Depends(require_device)])

    @router.get("/v1/agents")
    def read_agents(request: Request) -> Any:
        """报告模型提供方是否可用。

        @param request: 请求对象。
        @returns ``{checkedAt, providers}``；不可用时给出稳定原因码。
        """
        settings = request.app.state.settings
        snapshot = create_agent_availability_snapshot(
            configured=_is_agent_configured(settings),
            checked_at=datetime.now(UTC),
        )
        return {
            "checkedAt": snapshot.checked_at,
            "providers": [dict(provider) for provider in snapshot.providers],
        }

    @router.post("/v1/agent/sessions", status_code=201)
    async def create_session(request: Request) -> Any:
        """创建一个绑定首个数据基线的空会话。

        创建**不启动模型回合**。

        **参数来自请求体而不是查询串**，形状与参考实现一致：

        ```json
        {"provider": "genkit", "accountScope": {"kind": "account", "account": "<uuid>"}}
        ```

        ``accountScope`` 也可为 ``{"kind": "all"}``。``provider`` 只接受 ``"genkit"``。

        **这一点曾是真实缺陷**：此前参数走查询串（扁平 ``?accountId=``），而客户端按
        参考实现发的是请求体。结果是我的实现**完全忽略它**，``account_id`` 为 ``None``，
        于是"请求指定一个账户"变成了"绑定全部账户的会话"——**且不报错**。

        **数据截止时间取服务端时钟**，不接受客户端指定：会话基线必须与账本服务看到的
        时间一致，由客户端决定会让两侧对同一次读取产生不同的时点。

        @param request: 请求对象；正文为会话创建参数。
        @returns 201 与公开会话快照。
        @raises HTTPException: 模型未配置（503）、参数非法（400）、账户不存在（400）。
        """
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "Agent 会话创建参数无效"},
            ) from error
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "Agent 会话创建参数无效"},
            )

        provider = body.get("provider", "genkit")
        if provider != "genkit":
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "Agent 会话创建参数无效"},
            )

        # **`accountScope` 是必填的**（参考实现的 schema 无默认值）。
        #
        # 给它一个 "全部账户" 的默认值看起来友好，实际是把"客户端忘了带这个字段"
        # 变成"绑定全部账户"——**又是一次静默扩大数据范围**，正是本次修复要根除的东西。
        account_scope = body.get("accountScope")
        if not isinstance(account_scope, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "Agent 会话创建参数无效"},
            )
        kind = account_scope.get("kind")
        account_id = account_scope.get("account")
        if kind == "all":
            account_id = None
        elif kind == "account":
            # **必须显式给出账户 UUID**：``{"kind": "account"}`` 缺 ``account`` 是本实现
            # 最容易悄悄退化成"全部账户"的地方，因此直接拒绝。
            if not isinstance(account_id, str) or not _UUID_PATTERN.fullmatch(account_id):
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "INVALID_ARGUMENT",
                        "message": "Agent 会话创建参数无效",
                    },
                )
        else:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "Agent 会话创建参数无效"},
            )
        settings = request.app.state.settings
        if not _is_agent_configured(settings):
            raise HTTPException(
                status_code=503,
                detail={"code": "AGENT_UNAVAILABLE", "reason": "not_configured"},
            )
        connection = request.app.state.database.connection
        scope = AccountScope(kind="all") if not account_id else AccountScope(
            kind="account", account=account_id
        )
        if scope.kind == "account":
            # 会话里的账户是 UUID；不存在的 UUID 会让后续账本查询静默返回空。
            # **只接受 active 账户**：归档账户能建会话的话，后续每次工具调用都会
            # 静默返回空账本，用户看到"没有数据"而不是"账户已归档"。
            exists = connection.execute(
                "SELECT 1 FROM accounts WHERE id = CAST(? AS UUID) AND status = 'active'",
                [account_id],
            ).fetchone()
            if exists is None:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "INVALID_ARGUMENT",
                        "message": "账户不存在或已归档",
                    },
                )

        now = datetime.now(UTC)
        ledger_row = connection.execute(
            "SELECT coalesce(max(version), 0) FROM ledger_versions"
        ).fetchone()
        try:
            snapshot = create_agent_session(
                session_id=str(uuid.uuid4()),
                provider="genkit",
                availability=AgentProviderAvailability(
                    provider="genkit", status="available"
                ),
                account_scope=scope,
                ledger_version=int(ledger_row[0]) if ledger_row else 0,
                as_of=now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            )
        except Exception as error:
            raise HTTPException(
                status_code=400, detail={"code": "INVALID_ARGUMENT", "message": str(error)}
            ) from error

        DuckDbAgentSessionRepository(connection).create_session(
            snapshot, device_id=request.state.device.id, now=now
        )
        # **创建后立即登记运行态**：否则"建会话 → 订阅事件"的标准时序里，第一次订阅
        # 必然拿到 snapshot_required（运行态不存在），而参考实现此时会返回空事件列表。
        request.app.state.agent_orchestrator.register(
            snapshot, device_id=request.state.device.id
        )
        return {"session": project_agent_session(snapshot)}

    def _require_session_id(session_id: str) -> None:
        """校验路径里的会话 ID 是 UUID。

        **同时校验了三条按 ID 操作的路由**（读取 / 取消 / 发消息）：参考实现对路径参数
        用同一个 schema，因此格式非法时返回 **400** 而不是 404。

        这个区分对客户端有意义：400 说"你的请求构造错了"，404 说"这个资源不存在"——
        前者该改代码，后者该刷新列表。
        """
        if not _UUID_PATTERN.fullmatch(session_id):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "会话 ID 格式非法"},
            )

    @router.get("/v1/agent/sessions/{session_id}")
    def read_session(session_id: str, request: Request) -> Any:
        """读取会话快照与消息。

        @raises HTTPException: 会话不存在（404）。
        """
        _require_session_id(session_id)
        connection = request.app.state.database.connection
        row = DuckDbAgentSessionRepository(connection).load_session(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "AGENT_SESSION_NOT_FOUND"})
        messages = AgentMessageRepository(connection).load_messages(session_id)
        return {"session": project_agent_session(_rebuild(row, messages))}

    @router.get("/v1/agent/sessions/{session_id}/tool-calls")
    def read_tool_calls(session_id: str, request: Request) -> Any:
        """读取会话的工具调用审计记录。"""
        connection = request.app.state.database.connection
        calls = AgentMessageRepository(connection).load_tool_calls(session_id)
        # **信封键必须是 ``toolCalls``**：客户端读的是这个键（缺失时静默显示为空），
        # 而参考实现同样返回 ``{toolCalls: [...]}``。返回裸列表或 ``rows`` 会让
        # 审计页面永远空白且不报错。
        return {"toolCalls": [dict(call) for call in calls]}

    @router.post("/v1/agent/sessions/{session_id}/cancel")
    def cancel_session(session_id: str, request: Request) -> Any:
        """请求取消：把会话推进到 ``cancelling``。

        **真正的中止由编排器完成**，本端点只标记意图。客户端看到 ``cancelling``
        表示"请求已受理、尚未停止"，不应据此认为回合已经结束。
        """
        _require_session_id(session_id)
        result = _transition(request, session_id, "cancelling")
        # **取消意图由持久化状态表达**：编排器在收口前读 ``cancelling`` 决定按
        # ``cancelled`` 还是成功落库，因此这里不需要（也不能）依赖进程内标志。
        return result

    @router.post("/v1/agent/sessions/{session_id}/messages")
    async def send_message(session_id: str, request: Request) -> Any:
        """执行一个完整回合。

        @param session_id: 会话 ID。
        @param request: 请求对象；正文为 ``{"content": "..."}``。
        @returns 回合结束后的会话快照与本回合产生的事件。
        @raises HTTPException: 正文非法（400）、会话不存在（404）、
            状态冲突（409）、模型未配置（503）。
        """
        _require_session_id(session_id)
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(
                status_code=400, detail={"code": "INVALID_ARGUMENT"}
            ) from error
        content = body.get("content") if isinstance(body, dict) else None
        # **上限与参考实现一致（8192）。**
        #
        # 没有上限时，一条超大消息会一路进入模型调用与数据库写入——两侧对"多大算合法"
        # 的判断不同，而客户端的重试与截断逻辑正是按那个上限设计的。
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > _MAX_MESSAGE_LENGTH
        ):
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT"})

        connection = request.app.state.database.connection
        row = DuckDbAgentSessionRepository(connection).load_session(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "AGENT_SESSION_NOT_FOUND"})
        if row.status != "idle":
            # 回合边界之外不能发新消息——否则两个回合会交错。
            raise HTTPException(
                status_code=409,
                detail={"code": "AGENT_SESSION_STATE_CONFLICT", "from": row.status},
            )

        executor = request.app.state.agent_executor
        if executor is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "AGENT_UNAVAILABLE", "reason": "not_configured"},
            )

        orchestrator = request.app.state.agent_orchestrator
        messages = AgentMessageRepository(connection).load_messages(session_id)
        from ..application.agent_turn_orchestrator import REGISTRY_UNAVAILABLE
        orchestrator.register(
            _rebuild(row, messages),
            last_event_sequence=row.last_event_sequence,
            device_id=row.device_id,
            # 两条消息（用户 + 助手）接着既有行排下去：序号由运行态给出，
            # 避免在同一事务里再查一次 max(ordinal)。
            next_ordinal=len(messages) + 1,
        )
        try:
            result = orchestrator.run_turn(
                session_id=session_id, executor=executor, content=content
            )
        except ValueError as error:
            if str(error) == REGISTRY_UNAVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "AGENT_UNAVAILABLE", "reason": "not_configured"},
                ) from error
            raise HTTPException(
                status_code=409, detail={"code": "AGENT_SESSION_STATE_CONFLICT"}
            ) from error

        return {
            "session": project_agent_session(result.snapshot),
            "events": [_project_event(event) for event in result.events],
        }

    @router.delete("/v1/agent/sessions/{session_id}")
    def close_session(session_id: str, request: Request) -> Any:
        """关闭会话；只有 ``idle`` 会话可以关闭。"""
        return _transition(request, session_id, "closed")

    return router


def _transition(request: Request, session_id: str, target: str) -> Any:
    """按状态机推进会话，并以 CAS 落库。

    @raises HTTPException: 会话不存在（404）或转换非法（409）。
    """
    connection = request.app.state.database.connection
    repository = DuckDbAgentSessionRepository(connection)
    row = repository.load_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "AGENT_SESSION_NOT_FOUND"})

    snapshot = _rebuild(row, ())
    try:
        next_session = transition_agent_session(snapshot, target)  # type: ignore[arg-type]
    except AgentSessionStateError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": error.code,
                "from": error.from_status,
                "to": error.to_status,
            },
        ) from error
    if next_session is snapshot:
        # 幂等重放：已是目标状态，无需写库。
        return {"session": project_agent_session(snapshot)}
    if not repository.update_status(
        session_id, row.status, target, datetime.now(UTC)  # type: ignore[arg-type]
    ):
        raise HTTPException(
            status_code=409, detail={"code": "AGENT_SESSION_STATE_CONFLICT"}
        )
    messages = AgentMessageRepository(connection).load_messages(session_id)
    return {"session": project_agent_session(_rebuild(row, messages, status=target))}


def _project_event(event: Any) -> dict[str, Any]:
    """把事件投影为 camelCase 公开字段。

    ``payload`` **原样透传**：它已由领域层按事件类型构造，字段名各自约定
    （例如增量用 ``text``）。在这里改名会让客户端读不到增量。
    """
    return {
        "type": event.type,
        "sessionId": event.session_id,
        "messageId": event.message_id,
        "sequence": event.sequence,
        "occurredAt": event.occurred_at,
        "payload": dict(event.payload),
    }


def _rebuild(row: Any, messages: tuple, status: str | None = None) -> AgentSessionSnapshot:
    """把仓储行与消息还原为领域快照。

    **``account`` 是 UUID**（见 agent_session_repository 的说明），不是脱敏键。
    """
    return AgentSessionSnapshot(
        id=row.id, provider=row.provider, status=status or row.status,
        account_scope=row.account_scope, ledger_version=row.ledger_version,
        as_of=row.as_of, revision=row.revision, summary=row.summary,
        messages=tuple(
            _to_message(message) for message in messages
        ),
    )


def _to_message(message: Any):
    """把仓储消息还原为领域消息。

    **`streaming` 映射为"未设置"**：领域层的 `NormalizedAgentMessage.status` 只表达
    持久终态（completed / failed / cancelled）。仍在流式中的消息不该出现在会话快照里
    ——它是"正在进行"，由 WebSocket 事件表达，而不是快照的一部分。
    """
    from ..domain.agent.switching import NormalizedAgentMessage

    status = None if message.status == "streaming" else message.status
    return NormalizedAgentMessage(
        id=message.id, role=message.role, content=message.content,
        created_at=message.created_at, status=status,
        error_code=message.error_code,
    )


def _is_agent_configured(settings: Any) -> bool:
    """模型提供方是否已完整配置。

    **不发网络请求**：可用性端点会被客户端频繁轮询，每次都打上游既慢又会被限流。
    只检查三项配置是否齐全（缺一即视为未配置）。

    配置非法（例如 base URL 带凭据）时返回 ``False`` 而不是抛错：可用性端点的职责
    是回答"能不能用"，配置错误的具体原因由启动检查负责报告。
    """
    del settings
    import os

    from ..application.agent_config import load_genkit_config

    try:
        return load_genkit_config(os.environ) is not None
    except ValueError:
        return False
