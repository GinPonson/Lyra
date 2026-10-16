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

**每一条按 ID 的路由都按「当前用户 + 会话 ID」读取会话**：会话归属记在不可变的
``agent_session_identities`` 上，A 访问 B 的会话一律与"会话不存在"返回同一个 404，
历史消息与工具审计因此都不会泄漏（计划 §4.1/§4.2、V07）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from ..application.agent_availability import create_agent_availability_snapshot
from ..application.device_authenticator import user_disabled_error
from ..application.public_projection import project_agent_session
from ..domain.agent.lifecycle import AgentSessionStateError, transition_agent_session
from ..domain.agent.switching import (
    AgentProviderAvailability,
    AgentSessionSnapshot,
    create_agent_session,
)
from ..domain.analytics.types import AccountScope
from ..infrastructure.db.agent_message_repository import AgentMessageRepository
from ..infrastructure.db.agent_session_repository import (
    AgentSessionRow,
    DuckDbAgentSessionRepository,
)
from .dependencies import _UUID_PATTERN, require_user

# 单条消息上限，与参考实现的 schema 一致（8192 字符）。
_MAX_MESSAGE_LENGTH = 8_192


def create_agent_router() -> APIRouter:
    """构造 Agent 路由。

    @returns 能力发现端点；要求用户认证。
    """
    router = APIRouter(dependencies=[Depends(require_user)])

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
        user_id = request.state.auth.user_id
        scope = AccountScope(kind="all") if not account_id else AccountScope(
            kind="account", account=account_id
        )
        if scope.kind == "account":
            # 会话里的账户是 UUID；不存在的 UUID 会让后续账本查询静默返回空。
            # **只接受该用户自己的 active 账户**：账户条件必须叠加用户条件，否则 A 可以
            # 用 B 的账户 UUID 建会话，之后每次工具调用都按会话归属去读那个账户。
            # **只接受 active**：归档账户能建会话的话，后续每次工具调用都会静默返回空
            # 账本，用户看到"没有数据"而不是"账户已归档"。
            exists = connection.execute(
                """
                SELECT 1 FROM accounts
                WHERE id = CAST(? AS UUID) AND user_id = CAST(? AS UUID)
                  AND status = 'active'
                """,
                [account_id, user_id],
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
        # 账本基线版本按**当前用户**读取：全局 max(version) 会让 A 的会话拿到一个
        # 由 B 的导入推高的版本号（P0 契约 §3.3）。
        ledger_row = connection.execute(
            "SELECT coalesce(max(version), 0) FROM ledger_versions WHERE user_id = CAST(? AS UUID)",
            [user_id],
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
            snapshot,
            device_id=request.state.auth.device_id,
            # 归属写进不可变会话身份：它同时是之后每次读取与工具调用的授权依据。
            user_id=user_id,
            now=now,
        )
        # **创建后立即登记运行态**：否则"建会话 → 订阅事件"的标准时序里，第一次订阅
        # 必然拿到 snapshot_required（运行态不存在），而参考实现此时会返回空事件列表。
        request.app.state.agent_orchestrator.register(
            snapshot,
            device_id=request.state.auth.device_id,
            user_id=user_id,
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

        @raises HTTPException: 会话不存在或不属于当前用户（404）。
        """
        _require_session_id(session_id)
        connection = request.app.state.database.connection
        row = _owned_session(request, session_id)
        messages = AgentMessageRepository(connection).load_messages(session_id)
        return {"session": project_agent_session(_rebuild(row, messages))}

    @router.get("/v1/agent/sessions/{session_id}/tool-calls")
    def read_tool_calls(session_id: str, request: Request) -> Any:
        """读取会话的工具调用审计记录。

        **审计列表同样要校验归属**：它是会话内容的另一种视图，少了这一步，任何已认证
        用户都能凭一个会话 ID 读到别人问过什么、调了哪些工具（V07）。
        """
        _require_session_id(session_id)
        _owned_session(request, session_id)
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
        表示"请求已受理、尚未停止"。

        归属校验先于取消：编排器的运行态只按会话 ID 索引，若不在入口处校验，
        A 就能取消 B 正在跑的回合。
        """
        _require_session_id(session_id)
        _owned_session(request, session_id)
        snapshot = request.app.state.agent_orchestrator.request_cancel(
            session_id, now=datetime.now(UTC)
        )
        if snapshot is not None:
            return {"session": project_agent_session(snapshot)}
        # 尚无内存运行态的持久化会话也必须按同一状态机校验。
        return _transition(request, session_id, "cancelling")

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
        row = _owned_session(request, session_id)
        if row.status != "idle":
            # 回合边界之外不能发新消息——否则两个回合会交错。
            raise HTTPException(
                status_code=409,
                detail={"code": "AGENT_SESSION_STATE_CONFLICT", "from": row.status},
            )
        # **回合真正开始前再复核一次授权**：请求经过认证依赖之后、模型真正开跑之前可能
        # 已经被停用（停用与业务写入经同一写协调器排序，提交后排队任务一开始执行就必须
        # 被拒）。权限依赖只覆盖请求进入的那一刻，覆盖不了线程池排队与长回合（V11）。
        _require_active_user(connection, row.user_id)

        executor = request.app.state.agent_executor
        if executor is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "AGENT_UNAVAILABLE", "reason": "not_configured"},
            )

        orchestrator = request.app.state.agent_orchestrator
        messages = AgentMessageRepository(connection).load_messages(session_id)
        from ..application.agent_turn_orchestrator import REGISTRY_UNAVAILABLE
        try:
            orchestrator.register(
                _rebuild(row, messages),
                last_event_sequence=row.last_event_sequence,
                device_id=row.device_id,
                # 工具调用的用户范围只认会话身份；模型参数无法指定它。
                user_id=row.user_id,
                # 用户与助手消息接着既有行排列，运行态在开始回合时原子推进序号。
                next_ordinal=len(messages) + 1,
            )
            # 同步模型流必须在线程池执行，事件循环才能及时发送 WebSocket 与受理取消。
            result = await run_in_threadpool(
                orchestrator.run_turn,
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
        _require_session_id(session_id)
        _owned_session(request, session_id)
        return _transition(request, session_id, "closed")

    return router


def _owned_session(request: Request, session_id: str) -> AgentSessionRow:
    """按「当前用户 + 会话 ID」读取会话，读不到即 404。

    **归属校验落在仓储查询里**，而不是"取到行再比对 user_id"：比对写法只要漏一次就是
    一次越权，而带范围的查询漏不了。不属于当前用户的会话与不存在的会话返回同一个
    404，避免用错误码探测他人会话是否存在（计划 §4.2）。

    @param request: 请求对象；用户取自已认证上下文。
    @param session_id: 会话 ID（调用方需先校验 UUID 格式）。
    @returns 会话行；保证属于当前用户。
    @raises HTTPException: 会话不存在或不属于当前用户（404）。
    """
    connection = request.app.state.database.connection
    row = DuckDbAgentSessionRepository(connection).load_session_for_user(
        session_id, request.state.auth.user_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "AGENT_SESSION_NOT_FOUND"})
    return row


def _require_active_user(connection: Any, user_id: str) -> None:
    """确认该用户仍处于活动状态，否则按 ``USER_DISABLED`` 拒绝。

    令牌认证本身已经查过用户状态，但那发生在请求进入时。停用与业务写入走同一个写
    协调器，因此**停用提交之后才开始执行的排队任务必须在这里被拦下**（计划 §4.4、
    V11）——否则一个排在停用之前、执行在停用之后的回合仍会写入并投递数据。

    @param connection: 数据库连接。
    @param user_id: 待校验的用户 UUID 文本。
    @raises HTTPException: 用户不存在或已停用（403）。
    """
    row = connection.execute(
        "SELECT status FROM users WHERE id = CAST(? AS UUID)", [user_id]
    ).fetchone()
    if row is not None and row[0] == "active":
        return
    error = user_disabled_error()
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


def _transition(request: Request, session_id: str, target: str) -> Any:
    """按状态机推进会话，并以 CAS 落库。

    归属校验在这里再做一次（而不是只依赖调用方）：本函数同样可以被尚无内存运行态的
    路径调用，漏校验就等于给了另一条越权入口。

    @raises HTTPException: 会话不存在或不属于当前用户（404）、转换非法（409）。
    """
    connection = request.app.state.database.connection
    repository = DuckDbAgentSessionRepository(connection)
    row = _owned_session(request, session_id)

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
