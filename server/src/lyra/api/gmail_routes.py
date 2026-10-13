"""Gmail（Google 连接）路由。

**7 条路由**（不是 8 条——此前 §94.3 把 ``mappings`` 的两种方法重复计了一次）：

| 路由 | 说明 |
| --- | --- |
| ``POST /v1/google/authorization-transactions`` | 创建授权事务（201） |
| ``POST /v1/google/authorization-transactions/{state}/complete`` | 完成授权 |
| ``GET /v1/google/connections`` | 列出连接 |
| ``GET /v1/google/connections/{id}/mappings`` | 列出账户映射 |
| ``PUT /v1/google/connections/{id}/mappings/{mappingId}`` | 改绑映射 |
| ``POST /v1/google/connections/{id}/sync`` | 触发同步 |
| ``DELETE /v1/google/connections/{id}`` | 断开连接 |

**全部要求设备认证**：这是私有账本，连接与同步都只对已配对设备开放。

三处**在路由层**完成的格式校验（早于任何业务逻辑）：

- ``state`` 必须是 43 个 URL 安全字符（与生成时一致）；
- ``id`` / ``mappingId`` / ``explicitAccountId`` 必须是 UUID；
- ``confirmedRescanAfterDate`` 必须是 ``YYYY-MM-DD``。

放在路由层的价值是**错误信息能指明是哪一段**，而不是让下游抛一个笼统的"参数无效"。
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..application.app_error import ApplicationError
from .dependencies import require_device

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# 与授权事务生成时一致：32 字节 base64url（无填充）。
_STATE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_LOCAL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def create_gmail_router() -> APIRouter:
    """构造 Gmail 路由。

    @returns 含 7 条路由的路由器；全部要求设备认证。
    """
    router = APIRouter(dependencies=[Depends(require_device)])

    @router.post("/v1/google/authorization-transactions", status_code=201)
    def create_authorization_transaction(request: Request) -> Any:
        """创建一次 Google 授权事务。

        **事务绑定发起它的设备**：另一台设备拿到 state 也无法完成兑换。
        """
        device = _device(request)
        service = _service(request)
        transaction = service.create_authorization_transaction(device.id)
        if transaction is None:
            # Google OAuth 未配置：503 而不是 500——服务是好的，只是少了配置。
            raise HTTPException(
                status_code=503,
                detail={"code": "GMAIL_CONNECTION_ERROR", "message": "Google OAuth 客户端尚未配置"},
            )
        return transaction

    @router.post("/v1/google/authorization-transactions/{state}/complete")
    async def complete_authorization_transaction(state: str, request: Request) -> Any:
        """用授权码完成授权。

        正文为 ``{"code": "..."}``。
        """
        device = _device(request)
        if not _STATE.fullmatch(state):
            raise HTTPException(
                status_code=400, detail={"code": "INVALID_ARGUMENT", "message": "Google 授权事务标识无效"}
            )
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(
                status_code=400, detail={"code": "INVALID_ARGUMENT"}
            ) from error
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, str) or not 1 <= len(code.strip()) <= 1_024:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "Google 授权完成参数无效"},
            )
        return _call(
            _service(request).complete_authorization_transaction,
            device_id=device.id, state=state, authorization_code=code.strip(),
        )

    @router.get("/v1/google/connections")
    def list_connections(request: Request) -> Any:
        """列出已连接的 Google 账号。"""
        return {"connections": _service(request).list_connections()}

    @router.get("/v1/google/connections/{connection_id}/mappings")
    def list_mappings(connection_id: str, request: Request) -> Any:
        """列出某连接的账户映射。"""
        _require_uuid(connection_id, "Gmail 连接 ID 无效")
        return {"mappings": _service(request).list_mappings(connection_id)}

    @router.put("/v1/google/connections/{connection_id}/mappings/{mapping_id}")
    async def update_mapping(
        connection_id: str, mapping_id: str, request: Request
    ) -> Any:
        """改绑一条账户映射。

        正文为 ``{"accountId": "<uuid>"}``。
        """
        _require_uuid(connection_id, "Gmail 连接 ID 无效")
        _require_uuid(mapping_id, "Gmail 映射 ID 无效")
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(
                status_code=400, detail={"code": "INVALID_ARGUMENT"}
            ) from error
        account_id = body.get("accountId") if isinstance(body, dict) else None
        _require_uuid(account_id, "Gmail 映射更新参数无效")
        return {
            "mapping": _call(
                _service(request).update_mapping,
                connection_id=connection_id, mapping_id=mapping_id,
                target_account_id=account_id,
            )
        }

    @router.post("/v1/google/connections/{connection_id}/sync")
    async def sync_connection(connection_id: str, request: Request) -> Any:
        """触发一次同步。

        正文可选：``{"confirmedRescanAfterDate": "YYYY-MM-DD", "explicitAccountId": "<uuid>"}``。

        **``confirmedRescanAfterDate`` 由用户提供**：首次扫描或重扫的起始日期不能由
        服务端代选（§108.1）。
        """
        _require_uuid(connection_id, "Gmail 连接 ID 无效")
        raw = await _optional_json(request)
        after_date = raw.get("confirmedRescanAfterDate")
        explicit = raw.get("explicitAccountId")
        if after_date is not None and (
            not isinstance(after_date, str) or not _LOCAL_DATE.fullmatch(after_date)
        ):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "Gmail 同步参数无效"},
            )
        if explicit is not None:
            _require_uuid(explicit, "Gmail 同步参数无效")
        return _call(
            _service(request).sync,
            connection_id=connection_id,
            confirmed_rescan_after_date=after_date,
            explicit_account_id=explicit,
        )

    @router.delete("/v1/google/connections/{connection_id}")
    async def disconnect_connection(connection_id: str, request: Request) -> Any:
        """断开连接。

        **``revoke`` 来自请求体而不是查询串**（参考实现的 ``disconnectSchema``）。
        正文可为空，省略时按 ``false`` 处理。

        此前它读的是 ``?revoke=true``——一个按参考实现发正文的客户端会被**静默地**
        按"不撤销"处理：**连接确实断开了，但 Google 侧的授权仍然有效**。
        这类"部分生效且不报错"的差异比直接失败更难发现。

        ``revoke=true`` 时同时向 Google 撤销授权；**撤销失败会上报**，
        不会静默地把连接标成已断开（§112.4）。
        """
        _require_uuid(connection_id, "Gmail 连接 ID 无效")
        raw = await _optional_json(request)
        # ``z.coerce.boolean()`` 对缺失与假值一致，因此这里只认显式的真值。
        value = raw.get("revoke", False) if isinstance(raw, dict) else False
        revoke = value is True or (isinstance(value, str) and value.lower() == "true")
        return {
            "connection": _call(
                _service(request).disconnect,
                connection_id=connection_id, revoke=revoke,
            )
        }

    return router


def _device(request: Request) -> Any:
    """取出已认证设备。

    设备由路由级的 ``require_device`` 依赖写入 ``request.state``；这里直接读它，
    而不是再次调用依赖（那会因缺少授权头参数而失败）。

    @raises HTTPException: 依赖未生效（说明路由漏挂认证）。
    """
    device = getattr(request.state, "device", None)
    if device is None:
        raise HTTPException(
            status_code=401, detail={"code": "DEVICE_CREDENTIAL_INVALID", "message": "设备访问令牌无效"}
        )
    return device


def _service(request: Request) -> Any:
    """取出 Gmail 服务；未装配时报 503。"""
    service = getattr(request.app.state, "gmail_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "GMAIL_CONNECTION_ERROR", "message": "Google OAuth 客户端尚未配置"},
        )
    return service


def _call(action: Any, **kwargs: Any) -> Any:
    """执行服务动作并把应用错误翻译为 HTTP 响应。"""
    try:
        return action(**kwargs)
    except ApplicationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


async def _optional_json(request: Request) -> dict:
    """读取可选正文；缺失或非法时返回空对象。

    同步的所有参数都是可选的，因此"没有正文"是合法输入而不是错误。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 空正文或非 JSON 都按未提供处理
        return {}
    return body if isinstance(body, dict) else {}


def _require_uuid(value: Any, message: str) -> str:
    """校验 UUID 格式。

    @raises HTTPException: 格式不符（400）。
    """
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise HTTPException(
            status_code=400, detail={"code": "INVALID_ARGUMENT", "message": message}
        )
    return value
