"""认证路由。

闭合 §94 发现的路由缺口。**领域逻辑在 M5 已完整实现**（登录事务、设备会话、
刷新轮换），本模块只做 HTTP 挂载与装配。

| 路由 | 状态 |
| --- | --- |
| ``GET /v1/auth/providers`` | ✅ 可直接实现（目录） |
| ``POST /v1/devices/tokens`` | ✅ 可直接实现（刷新轮换） |
| ``POST /v1/auth/{provider}/start`` | ✅ 需要登录服务装配 |
| ``POST /v1/auth/{provider}/exchange`` | ✅ 同上 |
| ``GET /v1/auth/{provider}/callback`` | ✅ 同上，返回 HTML 页面 |
| ``POST /v1/auth/logout`` | ✅ 需要设备会话撤销 |

**这些路由不要求设备认证**（它们本身就是获取认证的入口）；设备令牌刷新由刷新凭据
证明身份，同样不需要访问令牌——**用过期或已撤销的访问令牌去刷新是行不通的**。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..application.app_error import ApplicationError
from ..application.device_pairing_service import DevicePairingService
from ..application.owner_login_providers import load_owner_login_providers
from ..application.owner_login_service import LoginStartInput, OwnerLoginService
from ..application.write_coordinator import WriteCoordinator
from ..infrastructure.auth.login_provider import (
    LOGIN_PROVIDER_IDS,
    LOGIN_PROVIDER_NAMES,
)
from ..infrastructure.db.device_repository import DuckDbDeviceRepository


def create_auth_router() -> APIRouter:
    """构造认证路由。

    @returns 登录目录与设备令牌刷新端点。
    """
    router = APIRouter()

    @router.get("/v1/auth/providers")
    def read_providers(request: Request) -> Any:
        """公开的登录来源目录。

        **可用性只看配置是否齐全**，不发网络请求：该端点会被登录页频繁调用。
        """
        configured = getattr(request.app.state, "login_provider_ids", frozenset())
        return {
            "providers": [
                {
                    "id": provider_id,
                    "name": LOGIN_PROVIDER_NAMES[provider_id],
                    "available": provider_id in configured,
                }
                for provider_id in LOGIN_PROVIDER_IDS
            ]
        }

    @router.post("/v1/devices/tokens")
    async def refresh_device_token(request: Request) -> Any:
        """用刷新凭据换一对新令牌。

        正文为 ``{"refreshToken": "..."}``。

        **成功响应必须给出新的刷新凭据**：它是一次性轮换，客户端若不保存新的那份，
        下一次刷新就会把旧的使用掉并被判定为重放。
        """
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(
                status_code=400, detail={"code": "INVALID_ARGUMENT"}
            ) from error
        token = body.get("refreshToken") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT"})

        service = DevicePairingService(
            DuckDbDeviceRepository(request.app.state.database.connection),
            WriteCoordinator(),
        )
        try:
            issued = await service.refresh_tokens(refresh_token=token)
        except ApplicationError as error:
            raise HTTPException(
                status_code=error.status_code, detail={"code": error.code, "message": error.message}
            ) from error
        return {
            "accessToken": issued.access_token,
            "accessTokenExpiresAt": issued.access_token_expires_at,
            "refreshToken": issued.refresh_token,
            "refreshTokenExpiresAt": issued.refresh_token_expires_at,
        }

    @router.post("/v1/auth/{provider}/start", status_code=201)
    async def start_login(provider: str, request: Request) -> Any:
        """发起登录。

        正文为 ``{displayName, publicKeyPem, codeChallenge, codeChallengeMethod}``。
        返回登录 ID 与授权地址。

        **响应带 ``cache-control: no-store``**：正文含 state 与授权地址，
        被缓存会让另一个请求复用同一事务。
        """
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(
                status_code=400, detail={"code": "INVALID_ARGUMENT"}
            ) from error
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT"})

        service = request.app.state.login_service
        try:
            started = service.start(
                provider,
                LoginStartInput(
                    display_name=body.get("displayName"),
                    public_key_pem=body.get("publicKeyPem"),
                    code_challenge=body.get("codeChallenge"),
                    code_challenge_method=body.get("codeChallengeMethod"),
                ),
                _source_key(request),
            )
        except ApplicationError as error:
            raise HTTPException(
                status_code=error.status_code, detail={"code": error.code, "message": error.message}
            ) from error

        response = JSONResponse({
            "loginId": started.login_id,
            "authorizationUrl": started.authorization_url,
            "expiresAt": started.expires_at,
            "pollIntervalSeconds": started.poll_interval_seconds,
        })
        response.headers["cache-control"] = "no-store"
        return response

    @router.get("/v1/auth/{provider}/callback")
    def complete_login_callback(
        provider: str, request: Request,
        state: str = Query(...),
        code: str | None = Query(default=None),
        error: str | None = Query(default=None),
    ) -> Any:
        """浏览器回调。

        app 与浏览器在此分离：**浏览器只完成身份验证**，Lyra 凭据始终经绑定 verifier
        的 POST 返回 App。因此本端点返回的 HTML **不含任何令牌**。

        @returns 固定文案的 HTML 页面。
        """
        service = request.app.state.login_service
        try:
            service.complete_callback(provider, state=state, code=code, error=error)
        except ApplicationError:
            # **无论成功失败都返回同一页面**：区分成功与失败会让未授权方凭页面内容
            # 判断某个 state 是否存在。
            return _callback_response()
        return _callback_response()

    # **返回类型必须是 None 而不是 Any。**
    #
    # FastAPI 会把返回注解当作 response_model；`Any` 会让 204 带上一个响应模型，
    # 于是**在路由注册时**断言失败（"Status code 204 must not have a response body"）
    # ——整个应用构造失败，全部请求一并报错。这个错误与 logout 本身毫无关系的外观
    # （190 项测试在 market / agent / gmail 各处报错）正是它发生在构造期而非请求期。
    @router.post("/v1/auth/logout", status_code=204)
    async def logout(request: Request) -> None:
        """退出登录：撤销**当前设备**及其全部凭据。

        需要访问令牌——撤销的目标由令牌本身确定，不接受客户端指定设备 ID。
        """
        authorization = request.headers.get("authorization")
        token = (
            authorization[len("Bearer ") :]
            if isinstance(authorization, str) and authorization.startswith("Bearer ")
            else None
        )
        if not token:
            raise HTTPException(
                status_code=401, detail={"code": "DEVICE_CREDENTIAL_INVALID", "message": "设备访问令牌无效"}
            )
        try:
            device = request.app.state.authenticator.authenticate(token)
        except ApplicationError as error:
            raise HTTPException(
                status_code=error.status_code, detail={"code": error.code, "message": error.message}
            ) from error

        pairing = DevicePairingService(
            DuckDbDeviceRepository(request.app.state.database.connection),
            WriteCoordinator(),
        )
        await pairing.revoke_device(device.id)
        # **204 无内容**：与参考实现一致。返回一个空 body 的 200 会让客户端以为
        # 响应里应该有内容——它可能去解析一个空字符串并因此报错。
        return None

    @router.post("/v1/auth/{provider}/exchange")
    async def exchange_login(provider: str, request: Request) -> Any:
        """用 App verifier 领取凭据（或得知仍在处理中）。

        正文为 ``{loginId, codeVerifier}``。

        **必须带 verifier**：只有 loginId 甚至浏览器 URL 都不足以领取凭据。
        未完成时返回 202（而不是错误），客户端据此继续轮询。
        """
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(
                status_code=400, detail={"code": "INVALID_ARGUMENT"}
            ) from error
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT"})

        service = request.app.state.login_service
        try:
            result = await service.exchange(
                provider,
                login_id=body.get("loginId"),
                code_verifier=body.get("codeVerifier"),
            )
        except ApplicationError as error:
            raise HTTPException(
                status_code=error.status_code, detail={"code": error.code, "message": error.message}
            ) from error

        pending = result.get("status") == "pending"
        payload = {"status": "pending"} if pending else {
            "status": "complete",
            "deviceId": result["deviceId"],
            "displayName": result.get("displayName"),
            "accessToken": result["accessToken"],
            "accessTokenExpiresAt": result["accessTokenExpiresAt"],
            "refreshToken": result["refreshToken"],
            "refreshTokenExpiresAt": result["refreshTokenExpiresAt"],
            "user": {
                "provider": result["user"].provider,
                "subject": result["user"].subject,
                "displayName": result["user"].display_name,
            },
        }
        response = JSONResponse(payload, status_code=202 if pending else 200)
        # 正文含凭据，绝不能被缓存。
        response.headers["cache-control"] = "no-store"
        return response

    return router


def _source_key(request: Request) -> str | None:
    """限流用的来源键。

    取客户端的直连地址；**不看 ``X-Forwarded-For``**：那个头可以由客户端伪造，
    用它做限流等于把预算交给攻击者。
    """
    return None if request.client is None else request.client.host


# 回调页面的安全响应头，与参考实现逐项一致。
#
# **这不是可选项**：该页面是返回给浏览器的 HTML，而这些头各自挡住一类真实攻击——
# 缺 CSP 的 `frame-ancestors` 会被套框（点击劫持），缺 `nosniff` 会被 MIME 嗅探，
# 缺 `no-store` 会让"一次性"的回调页留在浏览器缓存里。
_CALLBACK_HEADERS = {
    "cache-control": "no-store",
    "pragma": "no-cache",
    "referrer-policy": "no-referrer",
    "content-security-policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "x-content-type-options": "nosniff",
}


def _callback_response() -> HTMLResponse:
    """构造回调页面的响应（含全套安全头）。

    @returns 带安全头的 HTML 响应。
    """
    return HTMLResponse(_callback_page(), headers=_CALLBACK_HEADERS)


def _callback_page() -> str:
    """回调页面的固定文案。

    **不区分成功与失败**：区分会让未授权方凭页面内容判断某个 state 是否存在。
    真正的结果由 App 通过轮询获得。
    """
    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        "<title>Lyra</title></head><body>"
        "<p>已完成浏览器验证，请返回 Lyra App 继续。</p>"
        "</body></html>"
    )


def build_owner_login_service(environment: Any, device_authorizer: Any) -> OwnerLoginService:
    """按环境装配登录服务。

    **部分配置会让启动失败**（见 `load_owner_login_providers`）：让服务带着
    "看起来启用了但永远登不进去"的配置上线是最糟的选择。

    @param environment: 环境变量映射。
    @param device_authorizer: 登录成功后的设备签发用例。
    @returns 登录服务。
    """
    providers = load_owner_login_providers(environment)
    return OwnerLoginService(
        providers,
        device_authorizer,
        now=lambda: datetime.now(UTC),
    )


def _now() -> datetime:
    """服务端可信时间。"""
    return datetime.now(UTC)
