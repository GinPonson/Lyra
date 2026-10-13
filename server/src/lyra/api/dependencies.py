"""账号范围与设备认证的依赖装配。

**金额一律以十进制字符串出站**：FastAPI 的默认编码器会把 ``Decimal`` 转成
``float``，那会直接丢掉精确性，违反项目既定的金额传输规则。因此所有响应都先
经过 :func:`to_jsonable`，由 ``Decimal`` 显式转字符串。
"""

from __future__ import annotations

import re

from dataclasses import fields, is_dataclass
from decimal import Decimal
from typing import Any

from fastapi import Header, HTTPException, Request

from ..application.app_error import ApplicationError
from ..domain.analytics.types import AccountScope


def to_jsonable(value: Any) -> Any:
    """把领域对象递归转换为可 JSON 序列化的结构。

    @param value: 领域 dataclass、其容器或标量。
    @returns 相同结构的普通字典/列表；``Decimal`` 变为字符串。
    """
    if isinstance(value, Decimal):
        return format(value, "f")
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: to_jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


def require_device(request: Request, authorization: str | None = Header(default=None)) -> Any:
    """FastAPI 依赖：校验 `Authorization: Bearer` 并返回已认证设备。

    @param request: 请求对象，用于取出应用持有的认证器。
    @param authorization: 授权头原文。
    @returns 已认证设备的安全状态。
    @raises HTTPException: 缺少或非法令牌，统一返回 401。
    """
    if authorization is None or not authorization.startswith("Bearer ") or len(authorization) <= 7:
        raise HTTPException(status_code=401, detail={"code": "DEVICE_CREDENTIAL_INVALID", "message": "设备访问令牌无效"})
    token = authorization[len("Bearer ") :]
    authenticator = request.app.state.authenticator
    try:
        device = authenticator.authenticate(token)
    except ApplicationError as error:
        raise HTTPException(
            status_code=error.status_code, detail={"code": error.code, "message": error.message}
        ) from error
    # 也挂到 request.state：路由处理器常常需要设备身份（例如写 agent_sessions.device_id），
    # 而依赖的返回值只能被声明了该依赖的函数参数接收。
    request.state.device = device
    return device


def account_scope_of(value: str | None) -> AccountScope:
    """把查询参数解析为账户范围。

    @param value: ``all`` 或具体账户键；缺省为全部账户。
    @returns 显式账户范围。
    """
    if value is None or value == "" or value == "all":
        return AccountScope(kind="all")
    return AccountScope(kind="account", account=value)


_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def parse_scope_query(value: str | None) -> AccountScope:
    """解析 ``?scope=`` 查询参数。

    镜像 TypeScript 版 ``parseScopeQuery``。

    **"查全部账户"必须是一个显式的选择。** 缺参数时报 400 而不是默认"全部"——
    否则一个忘记带参数的客户端会拿到**全部账户的数据**，而不是一个错误。对私有账本
    应用，这个默认方向是反的。

    接受的取值：

    - ``all`` → 全部活跃账户；
    - 账户 UUID → 该账户。

    其余（含缺失）→ 400 ``ACCOUNT_SCOPE_REQUIRED``。

    @param value: 查询参数原文。
    @returns 账户范围。
    @raises HTTPException: 参数缺失或格式不符（400）。
    """
    if value == "all":
        return AccountScope(kind="all")
    if isinstance(value, str) and _UUID_PATTERN.fullmatch(value):
        return AccountScope(kind="account", account=value)
    raise HTTPException(
        status_code=400,
        detail={
            "code": "ACCOUNT_SCOPE_REQUIRED",
            "message": "请求必须明确指定单个账户或全部活跃账户",
        },
    )
