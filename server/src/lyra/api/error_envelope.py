"""统一的错误响应信封与请求 ID。

镜像 TypeScript 版 ``transport/api-error.ts`` 与 ``transport/http-server.ts`` 的
``setErrorHandler`` / ``genReqId``。

**错误体是扁平的，不是嵌套的**：

```json
{"request_id": "...", "code": "...", "message": "...", "details": null, "retryable": false}
```

三处容易忽略但影响很大的点：

1. **扁平而非 ``{"detail": {...}}``**——客户端读 ``body.code``，嵌套形状会让它读到
   ``undefined``；
2. **``retryable`` 必须发出**——它决定客户端是否重试。缺了它，客户端**无法区分
   可重试与永久失败**，只能一律重试或一律放弃；
3. **``request_id`` 用 snake_case**——它是日志关联标识，不是领域字段，因此不随
   "公开字段一律 camelCase"的约定。它同时通过 ``x-request-id`` 响应头暴露在**每个**
   响应上，便于用户报障时定位日志。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, TypeVar

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..application.app_error import ApplicationError

REQUEST_ID_HEADER = "x-request-id"

logger = logging.getLogger(__name__)

# 保留传入响应的具体类型（``JSONResponse`` 等），免得调用方的返回类型退化。
_ResponseT = TypeVar("_ResponseT", bound=Response)

# **禁止缓存的路径。** 这些响应含凭据或供应商密钥配置：
#
# | 路径 | 为什么不能缓存 |
# | --- | --- |
# | `/v1/auth/*` | 正文含 state 与授权地址 |
# | `POST /v1/devices/tokens` | 返回**访问令牌与刷新令牌** |
# | `/v1/market/settings` | 含行情提供方的 API 密钥配置 |
# | `POST /v1/ledger/accounts` | 创建账户的响应 |
#
# 被中间层或客户端缓存这些响应，等于让凭据在预期生命周期之外留存。
# 参考实现的覆盖是**两层**：一个 `/v1/auth/` 前缀的全局钩子 + 几处逐路由自设——
# 只实现前者会漏掉后三者（此前正是如此）。
# **不需要客户端协议头的 `/v1` 路由。**
#
# 它们要么是客户端在"还不知道服务端支持什么版本"时就要调的（`/v1/health`、
# `/v1/system/info`、`/v1/auth/providers`），要么是浏览器直接打开的
# （OAuth 回调页）——**那两种调用方都发不出这个头**。
_PUBLIC_V1_ROUTES = frozenset({
    "/v1/system/info",
    "/v1/health",
    "/v1/auth/google/callback",
    "/v1/auth/linuxdo/callback",
    "/v1/auth/github/callback",
    "/v1/auth/providers",
})

CLIENT_PROTOCOL_HEADER = "x-lyra-protocol-version"

_NO_STORE_PREFIXES = ("/v1/auth/", "/v1/market/settings")
_NO_STORE_EXACT = {"/v1/devices/tokens", "/v1/ledger/accounts"}

# 未归类异常对外的固定错误码与文案。
#
# **不把内部异常信息发出去**：异常文本可能包含文件路径、SQL 片段或账户标识。
# 真实原因写日志，响应只给一个稳定的码。
INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
INTERNAL_ERROR_MESSAGE = "服务端发生内部错误"


def _request_id(request: Request) -> str:
    """取出本请求的 ID；没有就生成一个。

    @param request: 当前请求。
    @returns 请求 ID。
    """
    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str) and existing != "":
        return existing
    generated = str(uuid.uuid4())
    request.state.request_id = generated
    return generated


def _finalize_response(request: Request, response: _ResponseT) -> _ResponseT:
    """为响应补上请求 ID（以及必要的禁止缓存）头。

    **必须逐个响应补，不能只写在中间件的 ``call_next`` 之后**：有两条路径根本走不到
    那里——协议门 426 在 ``call_next`` 之前就返回，未归类异常的 500 由 Starlette 的
    ``ServerErrorMiddleware`` 直接下发（异常从内层重新抛出，中间件没有机会处理响应）。
    只写一处会漏掉这两条，而它们恰恰是用户最需要凭 ID 报障的路径。

    @param request: 当前请求。
    @param response: 待发出的响应。
    @returns 同一个响应对象，便于链式返回。
    """
    response.headers[REQUEST_ID_HEADER] = _request_id(request)
    # 含凭据或供应商配置的响应禁止缓存。
    path = request.url.path
    if path in _NO_STORE_EXACT or path.startswith(_NO_STORE_PREFIXES):
        response.headers["cache-control"] = "no-store"
    return response


def error_body(
    request: Request,
    *,
    code: str,
    message: str,
    details: Any = None,
    retryable: bool = False,
) -> dict[str, Any]:
    """构造统一的错误响应体。

    @param request: 当前请求。
    @param code: 稳定的错误码。
    @param message: 面向用户的说明。
    @param details: 结构化补充信息；无则为 ``None``（**不是空对象**）。
    @param retryable: 客户端可否重试。
    @returns 扁平的错误体。
    """
    return {
        "request_id": _request_id(request),
        "code": code,
        "message": message,
        "details": details,
        "retryable": retryable,
    }


def install_error_envelope(app: FastAPI) -> None:
    """装配请求 ID 中间件与统一异常处理。

    @param app: 应用实例。
    """

    @app.middleware("http")
    async def assign_request_id(request: Request, call_next: Any) -> Any:
        """为每个请求生成 ID、校验客户端协议版本，并回写响应头。

        **即使响应成功也回写**：用户报障时给出的是这个值，没有它就无法从日志定位。
        """
        request.state.request_id = str(uuid.uuid4())

        # **协议门在业务之前。** 一个 App 版本与服务端不兼容时，让它先撞上"未认证"或
        # "参数错误"是误导——那些错误会让用户以为是自己操作错了。426 明确说"你的 App
        # 需要升级"，那是客户端唯一能据此采取正确动作的信号。
        gate = _client_protocol_gate(request)
        if gate is not None:
            return _finalize_response(request, gate)

        response = await call_next(request)
        return _finalize_response(request, response)

    @app.exception_handler(ApplicationError)
    async def handle_application_error(
        request: Request, error: ApplicationError
    ) -> JSONResponse:
        """领域错误：状态码、码、可重试性与明细都来自异常本身。"""
        return _finalize_response(request, JSONResponse(
            status_code=error.status_code,
            content=error_body(
                request,
                code=error.code,
                message=error.message,
                details=error.details,
                retryable=error.retryable,
            ),
        ))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        """路由层显式抛出的 ``HTTPException``。

        约定：``detail`` 是 ``{"code": ...}`` 时按该码发出；**其余键归入
        ``details``**——路由常把 ``reason`` / ``from`` 之类的补充信息直接写在
        ``detail`` 里，丢掉它们会让客户端只能看到"状态冲突"而不知道冲突在哪。
        非字典的 ``detail``（框架自身抛出的，例如 405）退化为按状态码推导的通用码。
        """
        detail = error.detail
        if isinstance(detail, dict) and "code" in detail:
            extra = {
                key: value for key, value in detail.items()
                if key not in ("code", "message", "details", "retryable")
            }
            explicit = detail.get("details")
            if explicit is None and extra:
                explicit = extra
            return _finalize_response(request, JSONResponse(
                status_code=error.status_code,
                content=error_body(
                    request,
                    code=str(detail["code"]),
                    message=str(detail.get("message", "")),
                    details=explicit,
                    retryable=bool(detail.get("retryable", False)),
                ),
            ))
        return _finalize_response(request, JSONResponse(
            status_code=error.status_code,
            content=error_body(
                request,
                code=_fallback_code(error.status_code),
                message=str(detail),
            ),
        ))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        """请求体/参数校验失败。

        参考实现对应 400 ``INVALID_ARGUMENT``；**明细附在 ``details`` 里**，
        便于客户端定位是哪个字段。
        """
        return _finalize_response(request, JSONResponse(
            status_code=400,
            content=error_body(
                request,
                code="INVALID_ARGUMENT",
                message="请求参数不符合公开定义",
                details={"errors": _jsonable_errors(error.errors())},
            ),
        ))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, error: Exception) -> JSONResponse:
        """未归类异常：**对外只说发生了内部错误，真实原因进日志**。

        异常文本可能包含文件路径、SQL 片段或账户标识，因此不发给客户端。
        **但它必须留下**：用户拿 ``request_id`` 报障时，日志是唯一能还原现场的记录，
        只回一个稳定的码而不记录异常，等于让这次故障无法排查。
        """
        logger.error(
            "未归类异常：request_id=%s error=%s: %s",
            _request_id(request),
            type(error).__name__,
            error,
            exc_info=error,
        )
        return _finalize_response(request, JSONResponse(
            status_code=500,
            content=error_body(
                request, code=INTERNAL_ERROR_CODE, message=INTERNAL_ERROR_MESSAGE
            ),
        ))


def _client_protocol_gate(request: Request) -> JSONResponse | None:
    """校验 ``x-lyra-protocol-version``，不通过时返回 426。

    适用范围与参考实现一致：所有 ``/v1/*``，**除公开路由**——那几条的调用方
    （健康探针、尚未得知服务端能力的客户端、浏览器里的 OAuth 回调页）发不出这个头。

    @param request: 当前请求。
    @returns 426 响应；通过时返回 ``None``。
    """
    from ..protocol import (
        MAX_SUPPORTED_PROTOCOL_VERSION,
        MIN_SUPPORTED_PROTOCOL_VERSION,
    )

    path = request.url.path
    if not path.startswith("/v1/") or path in _PUBLIC_V1_ROUTES:
        return None

    raw = request.headers.get(CLIENT_PROTOCOL_HEADER)
    try:
        version = int(raw) if raw is not None else None
    except ValueError:
        version = None
    if (
        version is None
        or not MIN_SUPPORTED_PROTOCOL_VERSION <= version <= MAX_SUPPORTED_PROTOCOL_VERSION
    ):
        return JSONResponse(
            status_code=426,
            content=error_body(
                request,
                code="CLIENT_VERSION_INCOMPATIBLE",
                message="App 与服务端协议版本不兼容，请升级后重试",
                details={
                    "min_supported_protocol_version": MIN_SUPPORTED_PROTOCOL_VERSION,
                    "max_supported_protocol_version": MAX_SUPPORTED_PROTOCOL_VERSION,
                },
            ),
        )
    return None


def _fallback_code(status_code: int) -> str:
    """把框架自身的状态码映射为通用错误码。

    @param status_code: HTTP 状态码。
    @returns 稳定的错误码。
    """
    return {
        401: "DEVICE_CREDENTIAL_INVALID",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
    }.get(status_code, "INVALID_ARGUMENT")


def _jsonable_errors(errors: list[Any]) -> list[dict[str, Any]]:
    """把 pydantic 的校验错误转为可序列化结构。

    ``ctx`` 里可能含异常对象，因此**逐字段挑取**而不是直接 ``json.dumps``。

    @param errors: ``RequestValidationError.errors()`` 的结果。
    @returns 可序列化的错误列表。
    """
    result: list[dict[str, Any]] = []
    for item in errors:
        if not isinstance(item, dict):
            continue
        result.append({
            "location": [str(part) for part in item.get("loc", ())],
            "message": str(item.get("msg", "")),
            "type": str(item.get("type", "")),
        })
    return result
