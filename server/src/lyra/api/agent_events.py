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
"""

from __future__ import annotations

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
        device = _authenticated_device(authenticator, token) if token else None
        if device is None:
            await websocket.close(code=UNAUTHORIZED_CLOSE_CODE, reason="unauthorized")
            return

        connections = websocket.app.state.event_connections
        connections.add(device.id, websocket)

        orchestrator = websocket.app.state.agent_orchestrator
        handle: int | None = None
        subscribed_session: str | None = None

        def push(event: Any) -> None:
            """把事件推给客户端。

            **推送前重新认证**：长连接不能沿用建立时的结果，否则设备被撤销后，只要
            客户端不再发帧，就能一直收到该账号的 Agent 事件（其中含账本增量）。
            参考实现同样在每次推送前认证。

            推送是同步回调，因此把发送排进事件循环；失败即认为连接已不可用。
            """
            import asyncio

            if not _authorized(authenticator, token):
                asyncio.ensure_future(_close_quietly(
                    websocket, UNAUTHORIZED_CLOSE_CODE, "signed out"
                ))
                return
            asyncio.ensure_future(_safe_send(websocket, {
                "type": "event",
                "event": {
                    "type": event.type,
                    "sessionId": event.session_id,
                    "messageId": event.message_id,
                    "sequence": event.sequence,
                    "occurredAt": event.occurred_at,
                    "payload": dict(event.payload),
                },
            }))

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
                resume = orchestrator.resume_events(
                    message.session_id, after_sequence=message.after_sequence
                ) if _registered(orchestrator, message.session_id) else None
                if resume is None:
                    await websocket.send_json({
                        "type": "subscribed",
                        "mode": "snapshot_required",
                        "reason": "SESSION_NOT_RESUMED",
                    })
                    continue
                # 重复订阅替换旧订阅，避免同一事件被推送多次。
                if handle is not None and subscribed_session is not None:
                    orchestrator.unsubscribe(subscribed_session, handle)
                handle = orchestrator.subscribe(message.session_id, push)
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
            connections.discard(websocket)

    return router


class EventConnectionRegistry:
    """设备 → 活动事件连接的注册表。

    **存在的理由**：退出登录除了撤销设备，还必须**踢掉该设备已经建立的事件流**——
    否则"已登出"的设备仍能持续收到 Agent 事件，直到它自己断开连接。
    """

    def __init__(self) -> None:
        self._by_device: dict[str, set[Any]] = {}

    def add(self, device_id: str, websocket: Any) -> None:
        """登记一个连接。"""
        self._by_device.setdefault(device_id, set()).add(websocket)

    def discard(self, websocket: Any) -> None:
        """移除一个连接；连接已不在时静默返回。"""
        for sockets in self._by_device.values():
            sockets.discard(websocket)
        for device_id in [key for key, value in self._by_device.items() if not value]:
            del self._by_device[device_id]

    def take_device(self, device_id: str) -> tuple[Any, ...]:
        """取出并移除某设备的全部连接。

        **只做"取走"，不负责关闭**：关闭是异步操作，而调用方（登出路由）是
        ``async def``——由它 ``await`` 才能真正关掉。注册表自己在同步上下文里
        排任务的话，既不保证连接被关掉，也会丢掉失败信息。

        @param device_id: 设备 ID。
        @returns 该设备的连接（已从注册表移除）。
        """
        return tuple(self._by_device.pop(device_id, ()))


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


def _registered(orchestrator: Any, session_id: str) -> bool:
    """会话是否已登记运行态。"""
    try:
        return orchestrator.runtime(session_id) is not None
    except Exception:  # noqa: BLE001
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
