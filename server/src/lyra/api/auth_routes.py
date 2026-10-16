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
| ``GET /v1/me`` | ✅ 当前设备会话的用户对象 |
| ``GET /v1/admin/users`` | ✅ 仅 admin |
| ``PATCH /v1/admin/users/{userId}`` | ✅ 仅 admin，首版只改状态 |

**这些路由不要求设备认证**（它们本身就是获取认证的入口）；设备令牌刷新由刷新凭据
证明身份，同样不需要访问令牌——**用过期或已撤销的访问令牌去刷新是行不通的**。
``/v1/me`` 与两条管理路由反过来**必须**要求用户认证（管理路由还要 admin），
它们通过端点级依赖声明，而不是路由级——路由级会把获取认证的入口一起挡住。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..application.app_error import ApplicationError
from ..application.device_pairing_service import DevicePairingService
from ..application.login_providers import load_login_providers
from ..application.login_service import LoginStartInput, UserLoginService
from ..infrastructure.auth.login_provider import (
    LOGIN_PROVIDER_IDS,
    LOGIN_PROVIDER_NAMES,
)
from ..infrastructure.db.device_repository import DuckDbDeviceRepository
from ..infrastructure.db.user_repository import (
    UserIdentityRecord,
    UserRecord,
    UserRepository,
)
from ..security.auth_context import AuthContext
from .dependencies import require_admin, require_user

# 400 的 message 与参考实现同文案。
#
# **它不是可选装饰**：错误信封只从 ``detail`` 取 message，缺了它客户端会拿到
# 一条空消息（界面上表现为一个没有内容的错误提示）。
_LOGIN_ARGUMENT_MESSAGE = "登录参数无效"
_DEVICE_TOKEN_MESSAGE = "设备令牌刷新参数无效"

# 登录发起正文必须齐备的字符串字段；顺序与 ``LoginStartInput`` 一致。
_START_TEXT_FIELDS = ("displayName", "publicKeyPem", "codeChallenge", "codeChallengeMethod")
# 兑换正文必须齐备的字符串字段。
_EXCHANGE_TEXT_FIELDS = ("loginId", "codeVerifier")

# 管理端用户列表的默认页大小。上限由 ``UserRepository`` 把守（越界抛 400 而不是静默截断）。
_DEFAULT_USER_PAGE_SIZE = 50
# 首版 ``PATCH`` 只接受状态变更；出现这两个字段即明确拒绝，而不是忽略。
_FORBIDDEN_ADMIN_PATCH_FIELDS = ("role", "identity", "identityId", "provider", "subject")


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
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": _DEVICE_TOKEN_MESSAGE},
            ) from error
        token = body.get("refreshToken") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": _DEVICE_TOKEN_MESSAGE},
            )

        # **写队列必须是应用级单例**：新建协调器会让健康端点的 ``writeQueue``
        # 与实际刷新路径看到两个互不相干的队列，等于没有串行化。
        service = DevicePairingService(
            DuckDbDeviceRepository(request.app.state.database.connection),
            request.app.state.write_coordinator,
            UserRepository(request.app.state.database.connection),
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
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": _LOGIN_ARGUMENT_MESSAGE},
            ) from error
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": _LOGIN_ARGUMENT_MESSAGE},
            )
        # **类型收口必须在服务层之前**：见 :func:`_required_text_fields`。
        fields = _required_text_fields(body, _START_TEXT_FIELDS)
        if fields is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": _LOGIN_ARGUMENT_MESSAGE},
            )

        service = request.app.state.login_service
        try:
            started = service.start(
                provider,
                LoginStartInput(
                    display_name=fields["displayName"],
                    public_key_pem=fields["publicKeyPem"],
                    code_challenge=fields["codeChallenge"],
                    code_challenge_method=fields["codeChallengeMethod"],
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
            raise _http_error(error) from error

        pairing = DevicePairingService(
            DuckDbDeviceRepository(request.app.state.database.connection),
            request.app.state.write_coordinator,
            UserRepository(request.app.state.database.connection),
        )
        await pairing.revoke_device(device.device_id)
        # 撤销设备只表示"以后不再认这枚凭据"；**已经建立的事件流与在跑的回合必须
        # 一起终止**，否则登出后服务端仍在替它产生并保存回答，且长连接继续收到账本
        # 增量——"已登出"就只是客户端的本地状态。
        #
        # 两处都用 ``getattr`` 容错：测试会挂载未装配这两个组件的应用，缺了它们不应
        # 让一次合法登出失败（撤销本身已经完成）。
        await _close_device_sessions(request, (device.device_id,))
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
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": _LOGIN_ARGUMENT_MESSAGE},
            ) from error
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": _LOGIN_ARGUMENT_MESSAGE},
            )
        # **类型收口先于"来源是否配置"**：格式非法的请求不该拿到 503。
        fields = _required_text_fields(body, _EXCHANGE_TEXT_FIELDS)
        if fields is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": _LOGIN_ARGUMENT_MESSAGE},
            )

        service = request.app.state.login_service
        try:
            result = await service.exchange(
                provider,
                login_id=fields["loginId"],
                code_verifier=fields["codeVerifier"],
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
            # 与 ``GET /v1/me`` **同一个** user 对象（契约 §3.9）：两处各自拼装
            # 必然会在某个字段上漂移，而客户端用同一个模型解析它们。
            "user": user_object(result["user"], result["identity"]),
        }
        response = JSONResponse(payload, status_code=202 if pending else 200)
        # 正文含凭据，绝不能被缓存。
        response.headers["cache-control"] = "no-store"
        return response

    @router.get("/v1/me")
    def read_me(
        request: Request, auth: AuthContext = Depends(require_user)
    ) -> Any:
        """当前登录用户的资料。

        身份**必须**来自当前设备会话的已验证关联（``paired_devices.identity_id``），
        不接受任何请求参数指定——那等于让调用方声明自己是谁（契约 §3.9）。

        @param auth: 已认证的用户上下文（由 ``require_user`` 注入并写入 ``request.state``）。
        @returns ``{"user": {...}}``，与登录完成响应中的 user 对象同形。
        @raises HTTPException: 用户或身份关联读不回来（401，数据不一致或已被清理）。
        """
        users = UserRepository(request.app.state.database.connection)
        devices = DuckDbDeviceRepository(request.app.state.database.connection)
        user = users.find_user_by_id(auth.user_id)
        identity = devices.load_device_identity(auth.device_id)
        if user is None or identity is None:
            # 令牌有效却读不到用户或身份：数据不一致，按未认证处理，不返回半个对象。
            raise HTTPException(
                status_code=401,
                detail={"code": "DEVICE_CREDENTIAL_INVALID", "message": "设备访问令牌无效"},
            )
        return {"user": user_object(user, identity)}

    @router.get("/v1/admin/users")
    def list_users(
        request: Request,
        limit: int = Query(default=_DEFAULT_USER_PAGE_SIZE),
        offset: int = Query(default=0),
        auth: AuthContext = Depends(require_admin),
    ) -> Any:
        """用户目录的受限分页（仅管理员）。

        **不返回账本、第三方令牌或凭据**：管理端只需要识别与停用用户。

        @param limit: 本页最大条数；越界由仓储抛 400，不静默截断。
        @param offset: 跳过条数。
        @param auth: 已认证的管理员上下文。
        @returns ``{"users": [...], "limit": ..., "offset": ...}``。
        """
        _ = auth  # 只为强制依赖生效；列表本身不需要调用者的身份。
        users = UserRepository(request.app.state.database.connection)
        try:
            records = users.list_users(limit=limit, offset=offset)
        except ApplicationError as error:
            raise _http_error(error) from error
        return {
            "users": [
                {
                    "userId": record.id,
                    "displayName": record.display_name,
                    "status": record.status,
                    "role": record.role,
                    "createdAt": record.created_at,
                    "updatedAt": record.updated_at,
                }
                for record in records
            ],
            "limit": limit,
            "offset": offset,
        }

    @router.patch("/v1/admin/users/{user_id}")
    async def update_user(
        user_id: str,
        request: Request,
        auth: AuthContext = Depends(require_admin),
    ) -> Any:
        """修改用户状态（仅管理员）。

        正文为 ``{"status": "active" | "disabled"}``。

        **首版只接受状态变更**：角色与身份不在本接口的可写范围。角色变更会直接改变
        谁能管理整个部署，把它塞进一个"改状态"的接口会让一次误调用静默提权；身份
        归属则必须由离线入口显式确认（契约 §3.10）。出现这些字段一律 400，而不是忽略。

        **不得停用最后一个活动管理员**：那会把部署锁死在无人可管理的状态，且没有
        任何 HTTP 入口能恢复（首个管理员只能由离线入口创建）。

        停用不只是改一个字段：同时撤销该用户全部设备与凭据，并关闭其事件流、终止
        在飞回合，否则"已停用"只对下一次请求生效（计划 §4.4）。

        @param user_id: 目标用户 UUID 文本。
        @param request: 请求对象，用于读正文与取出应用组件。
        @param auth: 已认证的管理员上下文。
        @returns 更新后的用户对象。
        @raises HTTPException: 正文非法（400）、用户不存在（404）、
            最后一个活动管理员（409）。
        """
        _ = auth
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "用户更新参数无效"},
            ) from error
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "用户更新参数无效"},
            )
        forbidden = sorted(key for key in body if key in _FORBIDDEN_ADMIN_PATCH_FIELDS)
        if forbidden:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_ARGUMENT",
                    "message": f"该接口只支持修改用户状态，不能修改 {'、'.join(forbidden)}",
                },
            )
        unknown = sorted(key for key in body if key != "status")
        if unknown or set(body) != {"status"}:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_ARGUMENT",
                    "message": "用户更新参数无效：只接受 status 字段",
                },
            )
        status = body["status"]
        if status not in ("active", "disabled"):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ARGUMENT", "message": "用户状态只能是 active 或 disabled"},
            )

        connection = request.app.state.database.connection
        users = UserRepository(connection)
        try:
            target = users.find_user_by_id(user_id)
            if target is None:
                raise ApplicationError(
                    code="USER_NOT_FOUND", message="用户不存在", status_code=404, retryable=False
                )
            # 判据取"当前仍是活动管理员"：已经停用的管理员再停用一次不该被拦下
            # （否则重复调用同一请求会得到一个与首次不同的结果）。
            if (
                status == "disabled"
                and target.role == "admin"
                and target.status == "active"
                and users.count_active_admins() <= 1
            ):
                raise ApplicationError(
                    code="LAST_ADMIN_PROTECTION",
                    message="不能停用最后一个活动管理员",
                    status_code=409,
                    retryable=False,
                )
            updated = users.set_user_status(user_id, status, now=datetime.now(UTC))
        except ApplicationError as error:
            raise _http_error(error) from error

        if status == "disabled" and target.status == "active":
            await _revoke_user_sessions(request, user_id)
        return {
            "user": {
                "userId": updated.id,
                "displayName": updated.display_name,
                "status": updated.status,
                "role": updated.role,
                "createdAt": updated.created_at,
                "updatedAt": updated.updated_at,
            }
        }

    return router


def _required_text_fields(body: Mapping[str, Any], keys: Sequence[str]) -> dict[str, str] | None:
    """按字段名取出必须为字符串的值；任一字段缺失或非字符串即返回 ``None``。

    **服务层假设这些字段已经是字符串**——它直接对值跑正则（``re.fullmatch``）与
    ``strip()``。把手写的 JSON dict 原样传下去，一个非字符串字段就会在服务层抛
    ``TypeError`` / ``AttributeError``，最终变成 500；而它本来是客户端的 400。

    参考实现在 transport 层用 zod schema 收口，这里保持同一道边界：**校验在
    进入服务层之前完成**，不用兜底 ``except Exception`` 掩盖。

    @param body: 已解析的 JSON 对象。
    @param keys: 必须存在的字符串字段名。
    @returns 字段名到字符串值的映射；校验失败时返回 ``None``。
    """
    values: dict[str, str] = {}
    for key in keys:
        value = body.get(key)
        if not isinstance(value, str):
            return None
        values[key] = value
    return values


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


def user_object(user: UserRecord, identity: UserIdentityRecord) -> dict[str, Any]:
    """把用户与其当前身份渲染为契约 §3.9 的 ``user`` 对象。

    **身份收在 ``identity`` 子对象里**，而不是与用户字段平铺：身份上的 ``display_name``
    是第三方昵称，与用户在 Lyra 的显示名不是一回事，平铺会让两者在同一个 key 上相撞。

    登录完成响应与 ``GET /v1/me`` **共用本函数**，从而不可能出现两个形状。

    @param user: 用户记录。
    @param identity: 本次已验证的身份记录；必须属于该用户。
    @returns 可直接序列化的 ``user`` 对象。
    """
    return {
        "userId": user.id,
        "displayName": user.display_name,
        "status": user.status,
        "role": user.role,
        "identity": {
            "provider": identity.provider,
            "subject": identity.subject,
            # email 可以是 null：OAuth 未提供邮箱不阻止注册（计划 §4.3），客户端按可空处理。
            "email": identity.email,
        },
    }


def _http_error(error: ApplicationError) -> HTTPException:
    """把应用错误转成同码同状态的 HTTP 错误。"""
    return HTTPException(
        status_code=error.status_code, detail={"code": error.code, "message": error.message}
    )


async def _close_device_sessions(request: Request, device_ids: Sequence[str]) -> None:
    """关闭这些设备的事件流并终止其在飞回合。

    停用用户与退出登录共用它：两者都要求"凭据不再可用"**和**"已经建立的长连接与
    在跑回合一起收口"。只做前者会让停用后的设备继续收到账本增量（计划 §4.4）。

    @param request: 请求对象，用于取出事件注册表与回合编排器。
    @param device_ids: 要收口的设备 ID。
    """
    connections = getattr(request.app.state, "event_connections", None)
    if connections is not None:
        # **在这里 await 关闭**：注册表只负责"取走连接"，真正关闭是异步操作；
        # 同步地排一个后台任务既不能保证连接被关掉，也会丢掉失败信息。
        from .agent_events import UNAUTHORIZED_CLOSE_CODE, _close_quietly

        for device_id in device_ids:
            for websocket in connections.take_device(device_id):
                await _close_quietly(websocket, UNAUTHORIZED_CLOSE_CODE, "signed out")
    orchestrator = getattr(request.app.state, "agent_orchestrator", None)
    if orchestrator is not None:
        for device_id in device_ids:
            orchestrator.cancel_device_turns(device_id, now=datetime.now(UTC))


async def _revoke_user_sessions(request: Request, user_id: str) -> None:
    """停用用户后的级联：撤销全部设备凭据，并关闭其事件流、终止在飞回合。

    @param request: 请求对象。
    @param user_id: 被停用的用户 UUID 文本。
    """
    pairing = DevicePairingService(
        DuckDbDeviceRepository(request.app.state.database.connection),
        request.app.state.write_coordinator,
        UserRepository(request.app.state.database.connection),
    )
    device_ids = await pairing.revoke_user_devices(user_id)
    await _close_device_sessions(request, device_ids)


def build_login_service(environment: Any, device_authorizer: Any) -> UserLoginService:
    """按环境装配登录服务。

    **部分配置会让启动失败**（见 `load_login_providers`）：让服务带着
    "看起来启用了但永远登不进去"的配置上线是最糟的选择。

    来源可用性只看 OAuth 凭据是否齐全；身份到用户的映射（含自动注册）由
    ``device_authorizer`` 在兑换成功时完成。

    @param environment: 环境变量映射。
    @param device_authorizer: 兑换成功后的用户解析与设备签发用例。
    @returns 登录服务。
    """
    providers = load_login_providers(environment)
    return UserLoginService(
        providers,
        device_authorizer,
        now=lambda: datetime.now(UTC),
    )


def _now() -> datetime:
    """服务端可信时间。"""
    return datetime.now(UTC)
