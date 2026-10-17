"""Agent 事件 WebSocket 端点。

镜像 TypeScript 版 ``transport/agent-events.ts``。

协议（与 TS 一致）：

```
客户端 → {"type": "subscribe", "sessionId": "<uuid>", "afterSequence": N}
服务端 → {"type": "subscribed", "mode": "events", "events": [...]}
     或 {"type": "subscribed", "mode": "snapshot_required", "reason": "..."}
随后   → {"type": "event", "event": {...}}
```

三条安全与正确性性质：

1. **每次消息与每次推送都重新认证。** 长连接不能沿用建立时的认证结果——否则设备
   撤销后旧的连接仍然收得到数据，直到客户端主动断开。
2. **本端点不缓存、不补造事件。** 序号有缺口一律让客户端重新取规范快照；
   服务端拼出来的增量会让客户端把不完整的文本当成最终答案。
3. **重复订阅替换旧订阅**，避免同一事件被推送多次。

**订阅还要校验会话归属**：认证只回答"你是谁"，不回答"这个会话是不是你的"。少了归属
校验，任何已认证用户只要拿到一个会话 ID 就能收到别人会话的实时增量与重放事件——认证
在这里是不够的（计划 §4.1、V07）。不属于当前用户的会话与不存在的会话返回**同一帧**，
避免把错误形状变成他人会话是否存在的探针。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# 未认证或认证失效时的关闭码，与 TypeScript 侧一致。
UNAUTHORIZED_CLOSE_CODE = 4001
INTERNAL_CLOSE_CODE = 1011


@dataclass(frozen=True)
class SubscribeMessage:
    """已校验的订阅帧。"""

    session_id: str
    after_sequence: int


def parse_subscribe_message(raw: str) -> SubscribeMessage | None:
    """解析订阅帧。

    @param raw: 客户端文本帧。
    @returns 已校验的订阅；格式非法返回 ``None``。
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or parsed.get("type") != "subscribe":
        return None
    session_id = parsed.get("sessionId")
    after_sequence = parsed.get("afterSequence")
    if not isinstance(session_id, str) or not _UUID.fullmatch(session_id):
        return None
    if (
        not isinstance(after_sequence, int)
        or isinstance(after_sequence, bool)
        or after_sequence < 0
    ):
        return None
    return SubscribeMessage(session_id=session_id, after_sequence=after_sequence)


def create_agent_events_router() -> APIRouter:
    """构造事件 WebSocket 路由。

    @returns 含 ``/v1/events`` 的路由器。
    """
    router = APIRouter()

    @router.websocket("/v1/events")
    async def agent_events(websocket: WebSocket) -> None:
        """Agent 事件流。

        连接建立后**第一件事是认证**；未通过则立即以 4001 关闭。
        """
        await websocket.accept()
        authenticator = websocket.app.state.authenticator
        token = _bearer_token(websocket.headers.get("authorization"))
        auth = _authenticated_device(authenticator, token) if token else None
        if auth is None:
            await websocket.close(code=UNAUTHORIZED_CLOSE_CODE, reason="unauthorized")
            return

        connections = websocket.app.state.event_connections
        # ``authenticate`` 返回的是 ``AuthContext``（含 user_id/device_id/role），
        # 不再是裸的设备记录——多用户改造后设备 id 要通过 ``device_id`` 取。
        # 同时按用户登记：停用用户时要能一次关掉他的全部连接，而不是先枚举设备。
        connections.add(auth.device_id, websocket, user_id=auth.user_id)

        orchestrator = websocket.app.state.agent_orchestrator
        connection = websocket.app.state.database.connection
        handle: int | None = None
        subscribed_session: str | None = None
        loop = asyncio.get_running_loop()
        pending_events: asyncio.Queue[tuple[int, Any]] = asyncio.Queue()
        send_lock = asyncio.Lock()
        subscription_generation = 0

        async def send_events() -> None:
            """按到达顺序推送事件；每次发送前在所属事件循环重新认证。"""
            while True:
                generation, event = await pending_events.get()
                async with send_lock:
                    # 重新订阅可能已重放这些事件；旧订阅排队中的帧不再发送。
                    if generation != subscription_generation:
                        continue
                    if not _authorized(authenticator, token):
                        await _close_quietly(websocket, UNAUTHORIZED_CLOSE_CODE, "signed out")
                        return
                    await _safe_send(websocket, {
                        "type": "event", "event": _event_payload(event),
                    })

        def push(event: Any, generation: int) -> None:
            """把工作线程中的事件送回连接所属循环，避免在线程池中创建异步任务。"""
            loop.call_soon_threadsafe(pending_events.put_nowait, (generation, event))

        sender = asyncio.create_task(send_events())
        try:
            while True:
                raw = await websocket.receive_text()
                # **重新认证**：长连接同样遵守凭据过期与设备撤销，不沿用建立时的结果。
                if not _authorized(authenticator, token):
                    await websocket.close(
                        code=UNAUTHORIZED_CLOSE_CODE, reason="unauthorized"
                    )
                    return
                message = parse_subscribe_message(raw)
                if message is None:
                    await websocket.send_json({
                        "type": "error",
                        "code": "INVALID_ARGUMENT",
                        "message": "WebSocket 订阅帧无效",
                    })
                    continue
                # **归属校验先于订阅**：认证只回答"你是谁"。不校验归属的话，任何已认证
                # 用户凭一个会话 ID 就能收到他人会话的重放与实时事件（V07）。
                #
                # 不属于当前用户的会话按"不可续传"回应——与不存在的会话**同一帧**，
                # 因此不能借此探测他人会话是否存在；同时也绝不建立订阅，历史与实时
                # 事件都不会流出。
                if not _session_belongs_to_user(connection, message.session_id, auth.user_id):
                    async with send_lock:
                        await websocket.send_json({
                            "type": "subscribed",
                            "mode": "snapshot_required",
                            "reason": "SESSION_NOT_RESUMED",
                        })
                    continue
                # 订阅确认帧必须先于新订阅的实时事件发送。
                async with send_lock:
                    # 旧订阅先移除，重放快照和新订阅在同一临界区建立，避免线程间事件缺口。
                    if handle is not None and subscribed_session is not None:
                        orchestrator.unsubscribe(subscribed_session, handle)
                    handle = None
                    subscribed_session = None
                    subscription_generation += 1
                    subscription = orchestrator.subscribe_with_resume(
                        message.session_id,
                        lambda event, generation=subscription_generation: push(event, generation),
                        after_sequence=message.after_sequence,
                    )
                    if subscription is None:
                        await websocket.send_json({
                            "type": "subscribed",
                            "mode": "snapshot_required",
                            "reason": "SESSION_NOT_RESUMED",
                        })
                        continue
                    handle, resume = subscription
                    subscribed_session = message.session_id
                    if resume.mode == "events":
                        await websocket.send_json({
                            "type": "subscribed",
                            "mode": "events",
                            "events": [_event_payload(event) for event in resume.events],
                        })
                    else:
                        await websocket.send_json({
                            "type": "subscribed",
                            "mode": "snapshot_required",
                            "reason": resume.reason,
                        })
        except WebSocketDisconnect:
            pass
        finally:
            if handle is not None and subscribed_session is not None:
                orchestrator.unsubscribe(subscribed_session, handle)
            sender.cancel()
            try:
                await sender
            except asyncio.CancelledError:
                pass
            connections.discard(websocket)

    return router


class EventConnectionRegistry:
    """设备/用户 → 活动事件连接的注册表。

    **存在的理由**：退出登录除了撤销设备，还必须**踢掉该设备已经建立的事件流**——
    否则"已登出"的设备仍能持续收到 Agent 事件，直到它自己断开连接。

    两个索引：设备维度用于"退出登录只关这一台"，用户维度用于"停用用户一次关掉他的
    全部连接"。**不合并成一个**：用用户维度关单台设备会误伤同用户其它设备的对话，
    而用设备维度做停用级联则要先枚举设备，漏一台就留下一条继续收数据的连接。
    """

    def __init__(self) -> None:
        self._by_device: dict[str, set[Any]] = {}
        self._by_user: dict[str, set[Any]] = {}

    def add(self, device_id: str, websocket: Any, *, user_id: str | None = None) -> None:
        """登记一个连接。

        @param device_id: 连接所属设备（凭据撤销的粒度）。
        @param websocket: 连接对象。
        @param user_id: 连接所属用户（停用级联的粒度）。WS 入口总是给出；缺省时该连接
            只登记在设备索引下——设备级撤销仍能覆盖它（``revoke_user_devices`` 会枚举
            该用户的全部设备），但按用户一次收口需要它。
        """
        self._by_device.setdefault(device_id, set()).add(websocket)
        if user_id is not None:
            self._by_user.setdefault(user_id, set()).add(websocket)

    def discard(self, websocket: Any) -> None:
        """移除一个连接；连接已不在时静默返回。"""
        for sockets in self._by_device.values():
            sockets.discard(websocket)
        for sockets in self._by_user.values():
            sockets.discard(websocket)
        for device_id in [key for key, value in self._by_device.items() if not value]:
            del self._by_device[device_id]
        for user_id in [key for key, value in self._by_user.items() if not value]:
            del self._by_user[user_id]

    def take_device(self, device_id: str) -> tuple[Any, ...]:
        """取出并移除某设备的全部连接。

        **只做"取走"，不负责关闭**：关闭是异步操作，而调用方（登出路由）是
        ``async def``——由它 ``await`` 才能真正关掉。注册表自己在同步上下文里
        排任务的话，既不保证连接被关掉，也会丢掉失败信息。

        @param device_id: 设备 ID。
        @returns 该设备的连接（已从注册表移除）。
        """
        return self._take(self._by_device, device_id)

    def take_user(self, user_id: str) -> tuple[Any, ...]:
        """取出并移除某用户的全部连接（停用级联用）。

        与 :meth:`take_device` 同样是"取走不关闭"，并**同时**从设备索引移除，避免
        取走后设备索引里还留着已经不属于任何人的引用。

        @param user_id: 用户 UUID 文本。
        @returns 该用户的连接（已从注册表移除）。
        """
        return self._take(self._by_user, user_id)

    def _take(self, index: dict[str, set[Any]], key: str) -> tuple[Any, ...]:
        """从指定索引取走并清理另一个索引中的同一批连接。"""
        sockets = tuple(index.pop(key, ()))
        other = self._by_device if index is self._by_user else self._by_user
        for websocket in sockets:
            for owner in [item for item, value in other.items() if websocket in value]:
                other[owner].discard(websocket)
                if not other[owner]:
                    del other[owner]
        return sockets


def _bearer_token(header: str | None) -> str | None:
    """从 Authorization 头取出令牌。"""
    if header is None or not header.startswith("Bearer "):
        return None
    token = header[len("Bearer ") :]
    return token or None


def _authenticated_device(authenticator: Any, token: str) -> Any | None:
    """认证令牌并返回设备；失败返回 ``None``。"""
    from ..application.app_error import ApplicationError

    try:
        return authenticator.authenticate(token)
    except ApplicationError:
        return None
    except Exception:  # noqa: BLE001 - 认证实现异常同样视为未认证
        return None


def _session_belongs_to_user(connection: Any, session_id: str, user_id: str) -> bool:
    """判断会话是否属于该用户。

    复用会话仓储的带范围读取（:meth:`DuckDbAgentSessionRepository.load_session_for_user`）
    而不是在这里再写一条 SQL：归属规则只有一处，两边写法分叉就会出现"HTTP 拒绝、
    WS 放行"的缺口。查不到（不存在或不属于该用户）一律返回 ``False``。

    @param connection: 数据库连接。
    @param session_id: 会话 ID。
    @param user_id: 当前用户 UUID 文本。
    @returns 会话属于该用户时为 ``True``。
    """
    from ..infrastructure.db.agent_session_repository import (
        DuckDbAgentSessionRepository,
    )

    return (
        DuckDbAgentSessionRepository(connection).load_session_for_user(session_id, user_id)
        is not None
    )


def _authorized(authenticator: Any, token: str) -> bool:
    """认证令牌；失败返回 ``False`` 而不是抛错——调用方据此关闭连接。"""
    from ..application.app_error import ApplicationError

    try:
        authenticator.authenticate(token)
        return True
    except ApplicationError:
        return False
    except Exception:  # noqa: BLE001 - 认证实现异常同样视为未认证
        return False


def _event_payload(event: Any) -> dict[str, Any]:
    """把领域事件投影为公开字段。"""
    return {
        "type": event.type,
        "sessionId": event.session_id,
        "messageId": event.message_id,
        "sequence": event.sequence,
        "occurredAt": event.occurred_at,
        "payload": dict(event.payload),
    }


async def _close_quietly(websocket: WebSocket, code: int, reason: str) -> None:
    """关闭连接；已经关闭或出错时静默忽略。"""
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 - 连接已断开
        return


async def _safe_send(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """发送 JSON；连接已关闭或出错时静默忽略。

    被动推送失败不应让服务端崩溃——客户端断开是常态。
    """
    try:
        await websocket.send_json(payload)
    except Exception:  # noqa: BLE001 - 连接已断开
        return
