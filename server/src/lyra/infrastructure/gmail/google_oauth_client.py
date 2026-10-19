"""Google OAuth 客户端。

镜像 TypeScript 版 ``infrastructure/gmail/google-oauth-client.ts`` 的端点常量与
授权请求构造部分。

**`access_type=offline` 与 `prompt=consent` 必须成对出现。** 缺任何一个，Google 都
不会返回 refresh token：

- 只有 `access_type=offline` 而没有 `prompt=consent`：**第二次授权**不再返回
  refresh token（Google 认为已有授权）；
- 只有 `prompt=consent` 而没有 `access_type=offline`：返回的令牌不含离线能力。

后果是连接在 access token 过期后**再也无法刷新**——用户会看到"连接突然失效"，
而重新授权一次又会短暂可用，如此反复。

`include_granted_scopes=true` 启用增量授权：用户之前授予的其它权限被保留。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlparse

from .oauth_transaction import (
    GMAIL_READONLY_SCOPE,
    CreatedGoogleOAuthTransaction,
    GmailOAuthError,
    assert_gmail_readonly_scope,
)
from .http_transport import GoogleEndpointFailure

GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_TOKEN_REVOCATION_ENDPOINT = "https://oauth2.googleapis.com/revoke"
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"

GOOGLE_GMAIL_AUTHORIZATION_SCOPES: tuple[str, ...] = (
    "openid", "email", GMAIL_READONLY_SCOPE,
)

# 单次 HTTP 调用超时：避免授权请求无限占用服务端任务。
GOOGLE_OAUTH_TIMEOUT_MILLISECONDS = 15_000

# state 与 PKCE challenge 都是 32 字节的 base64url（无填充）→ 恰好 43 个字符。
_BASE64URL_43 = re.compile(r"^[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True)
class GoogleAuthorizationRequest:
    """授权请求的公开参数。"""

    authorization_url: str
    expires_at: str


def build_google_authorization_request(
    transaction: CreatedGoogleOAuthTransaction,
    *,
    client_id: str,
    redirect_uri: str,
) -> GoogleAuthorizationRequest:
    """构造 Google 授权地址。

    @param transaction: 已创建的授权事务。
    @param client_id: OAuth client ID。
    @param redirect_uri: 回调地址。
    @returns 授权地址与事务过期时间。
    @raises ValueError: 参数缺失、state/challenge 形状不符或时间无效。
    """
    normalized_client_id = _require_text(client_id, "Google OAuth client ID")
    normalized_redirect = _require_redirect_uri(redirect_uri)
    if not _BASE64URL_43.fullmatch(transaction.state):
        raise ValueError("Google OAuth state 无效")
    if (
        transaction.code_challenge_method != "S256"
        or not _BASE64URL_43.fullmatch(transaction.code_challenge)
    ):
        raise ValueError("Google OAuth PKCE challenge 无效")
    if not _is_valid_timestamp(transaction.record.expires_at):
        raise ValueError("Google OAuth 授权事务过期时间无效")

    params = {
        "client_id": normalized_client_id,
        "redirect_uri": normalized_redirect,
        "response_type": "code",
        # 作用域以空格分隔，与 Google 约定一致。
        "scope": " ".join(GOOGLE_GMAIL_AUTHORIZATION_SCOPES),
        "state": transaction.state,
        "code_challenge": transaction.code_challenge,
        "code_challenge_method": transaction.code_challenge_method,
        # **这一对缺一不可**：否则第二次授权不再返回 refresh token。
        "access_type": "offline",
        "prompt": "consent",
        # 保留用户此前授予的其它权限。
        "include_granted_scopes": "true",
    }
    return GoogleAuthorizationRequest(
        authorization_url=f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{urlencode(params)}",
        expires_at=transaction.record.expires_at,
    )


def _require_text(value: str, label: str) -> str:
    """去除边界空白并拒绝空值。"""
    if not isinstance(value, str):
        raise ValueError(f"{label}不能为空")
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{label}不能为空")
    return normalized


def _require_redirect_uri(value: str) -> str:
    """校验回调地址。

    **不得含凭据或 fragment**：凭据会随授权地址外泄；fragment 会被浏览器剥离，
    导致回调静默缺少参数。

    @raises ValueError: 无法解析或含凭据/fragment。
    """
    normalized = _require_text(value, "Google OAuth redirect URI")
    parsed = urlparse(normalized)
    if parsed.scheme == "" or parsed.netloc == "":
        raise ValueError("Google OAuth redirect URI 无效")
    if parsed.username or parsed.password:
        raise ValueError("Google OAuth redirect URI 不能包含凭据或 fragment")
    if parsed.fragment:
        raise ValueError("Google OAuth redirect URI 不能包含凭据或 fragment")
    return normalized


def _is_valid_timestamp(value: str) -> bool:
    """判断 ISO 时间戳是否可解析。"""
    from datetime import datetime

    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    return True


def calculate_access_token_expiry(now: datetime, expires_in_seconds: int) -> str | None:
    """计算 access token 到期时刻。

    @param now: 服务端可信时间。
    @param expires_in_seconds: Google 返回的有效秒数。
    @returns ISO UTC 文本；无法计算时 ``None``（调用方据此判定响应畸形）。
    """
    from datetime import timedelta

    try:
        expires_at = now + timedelta(seconds=expires_in_seconds)
    except (OverflowError, TypeError, ValueError):
        return None
    return (
        expires_at.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def token_endpoint_failure_code(status: int | None) -> str:
    """把令牌端点的失败状态映射为同步错误码。

    **400 / 401 / 403 表示授权已失效**，需要用户重新授权。把它们当成暂时性故障会让
    客户端对着一个已经作废的凭据无限重试——而每次重试都不会成功。

    @param status: HTTP 状态；``None`` 表示传输层失败。
    @returns ``GMAIL_AUTH_REVOKED`` 或 ``GMAIL_CONNECTION_ERROR``。
    """
    if status in (400, 401, 403):
        return "GMAIL_AUTH_REVOKED"
    return "GMAIL_CONNECTION_ERROR"


def decrypt_connection_refresh_token(connection: object, cipher: object) -> str:
    """解密连接的 refresh token。

    **两类失败对应两种补救方式**：

    - 连接已断开或没有密文 → ``GMAIL_AUTH_REVOKED``：用户重新授权即可；
    - 解密失败 → ``GMAIL_CONNECTION_ERROR``：问题在**服务端密钥**，让用户反复重新
      授权是徒劳的，必须先去检查密钥配置。

    把两者混为一谈会让真正的密钥问题被掩盖在一连串"请重新登录"的提示后面。

    @param connection: 连接记录。
    @param cipher: 加密器。
    @returns refresh token 明文。
    @raises GmailSyncError: 上述两类失败。
    """
    from ...domain.gmail.sync_plan import GmailSyncError

    if (
        getattr(connection, "status", None) == "disconnected"
        or getattr(connection, "encrypted_refresh_token", None) is None
    ):
        raise GmailSyncError("GMAIL_AUTH_REVOKED", "Gmail 连接已断开，需要重新授权")
    try:
        return cipher.decrypt(  # type: ignore[attr-defined]
            connection.encrypted_refresh_token,  # type: ignore[attr-defined]
            f"gmail-connection:{connection.id}",  # type: ignore[attr-defined]
        )
    except Exception as error:
        raise GmailSyncError(
            "GMAIL_CONNECTION_ERROR",
            "存储的 Gmail 凭据无法解密，需要检查服务端密钥或重新连接",
        ) from error


def build_token_exchange_form(
    *,
    authorization_code: str,
    code_verifier: str,
    client_id: str,
    redirect_uri: str,
    client_secret: str | None = None,
) -> dict[str, str]:
    """构造授权码交换的表单。

    @param authorization_code: Google 授权码。
    @param code_verifier: PKCE verifier。
    @param client_id: OAuth client ID。
    @param redirect_uri: 回调地址（必须与授权时一致）。
    @param client_secret: 可选密钥。
    @returns 表单字段。
    @raises ValueError: 必填参数为空。
    """
    form = {
        "code": _require_text(authorization_code, "Google 授权码"),
        "code_verifier": _require_text(code_verifier, "Google PKCE verifier"),
        "client_id": _require_text(client_id, "Google OAuth client ID"),
        "redirect_uri": _require_text(redirect_uri, "Google OAuth redirect URI"),
        "grant_type": "authorization_code",
    }
    if client_secret is not None:
        form["client_secret"] = _require_text(client_secret, "Google OAuth client secret")
    return form


def build_token_refresh_form(
    *, refresh_token: str, client_id: str, client_secret: str | None = None
) -> dict[str, str]:
    """构造令牌刷新的表单。

    **刷新不需要 ``redirect_uri``**：那个参数只在授权码交换时用于校验来源。

    @raises ValueError: 必填参数为空。
    """
    form = {
        "refresh_token": _require_text(refresh_token, "Google refresh token"),
        "client_id": _require_text(client_id, "Google OAuth client ID"),
        "grant_type": "refresh_token",
    }
    if client_secret is not None:
        form["client_secret"] = _require_text(client_secret, "Google OAuth client secret")
    return form


@dataclass(frozen=True)
class GoogleTokenResponse:
    """授权码兑换成功后返回的令牌字段。"""

    access_token: str
    refresh_token: str
    granted_scope_text: str
    expires_in_seconds: int


def parse_google_token_response(payload: object) -> GoogleTokenResponse:
    """解析授权码兑换响应；该流程必须返回离线 refresh token 与 granted scope。

    **``token_type`` 必须是 ``bearer``（不区分大小写）。** 其它类型说明响应不是我们
    预期的形状；把它当 Bearer 令牌用，会在下一次调用时报出一个与根因无关的 401。

    **``expires_in`` 必须是正安全整数**：零或负值会让令牌"生下来就过期"，每次请求都
    刷新一次。

    @param payload: 已解析的 JSON 响应。
    @returns 令牌字段。
    @raises GmailOAuthError: 形状不符。
    """
    if not isinstance(payload, dict):
        raise _malformed_token_response()
    token_type = payload.get("token_type")
    expires_in = payload.get("expires_in")
    if (
        not isinstance(token_type, str)
        or token_type.lower() != "bearer"
        or not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or expires_in <= 0
    ):
        raise _malformed_token_response()
    return GoogleTokenResponse(
        access_token=_read_text(payload, "access_token"),
        refresh_token=_read_text(payload, "refresh_token"),
        granted_scope_text=_read_text(payload, "scope"),
        expires_in_seconds=expires_in,
    )


@dataclass(frozen=True)
class GoogleRefreshResponse:
    """刷新 access token 的响应字段；Google 不要求返回 refresh token。"""

    access_token: str
    granted_scope_text: str | None
    expires_in_seconds: int


def parse_google_refresh_response(payload: object) -> GoogleRefreshResponse:
    """解析刷新响应，并允许 Google 省略 refresh token 或未变化的 scope。

    Google 的刷新请求沿用服务器已保存的 refresh token；成功响应要求 access token、
    token 类型与有效期。若响应省略 scope，则调用方沿用此前已验证的持久化范围。

    @param payload: 已解析的 JSON 响应。
    @returns 新 access token、可选 scope 与有效秒数。
    @raises GmailOAuthError: 响应形状不符。
    """
    if not isinstance(payload, dict):
        raise _malformed_token_response()
    token_type = payload.get("token_type")
    expires_in = payload.get("expires_in")
    if (
        not isinstance(token_type, str)
        or token_type.lower() != "bearer"
        or not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or expires_in <= 0
    ):
        raise _malformed_token_response()
    scope = payload.get("scope")
    if scope is not None and (not isinstance(scope, str) or scope.strip() == ""):
        raise _malformed_token_response()
    return GoogleRefreshResponse(
        access_token=_read_text(payload, "access_token"),
        granted_scope_text=None if scope is None else scope.strip(),
        expires_in_seconds=expires_in,
    )


def parse_verified_identity(payload: object) -> tuple[str, str]:
    """解析并校验 userinfo 响应。

    **``email_verified`` 必须是布尔 ``true``**，不是"真值"。Google 文档明确该字段是
    布尔；接受 ``"true"`` 或 ``1`` 会让"未验证邮箱"在某些响应形状下被当成已验证——
    而邮箱是账户归属的依据。

    @param payload: 已解析的 JSON 响应。
    @returns ``(subject, email)``；邮箱已小写化。
    @raises GmailOAuthError: 身份未验证或字段缺失。
    """
    if not isinstance(payload, dict):
        raise _unverified_identity()
    email = str(payload.get("email") or "").strip().lower()
    # 严格等于 True：`1` 与 `"true"` 都不算。
    if payload.get("email_verified") is not True or not _EMAIL.fullmatch(email):
        raise _unverified_identity()
    return _read_text(payload, "sub"), email


def google_endpoint_error(endpoint: str, status: int) -> GmailOAuthError:
    """把 Google 端点的 HTTP 失败映射为可预期错误。

    **只有 429 与 5xx 可重试**：4xx 重试不会改变结果。

    @param endpoint: ``token`` 或 ``userinfo``，用于区分提示文案。
    @param status: HTTP 状态码。
    @returns 可预期错误。
    """
    message = (
        "无法完成 Google 授权，请重新授权或稍后重试"
        if endpoint == "token"
        else "无法验证 Google 账户身份，请重新授权或稍后重试"
    )
    return GmailOAuthError(
        "GOOGLE_TOKEN_EXCHANGE_FAILED", message,
        retryable=status == 429 or status >= 500,
    )


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")


def _read_text(payload: dict, field: str) -> str:
    """读取必填非空字符串。"""
    value = payload.get(field)
    if not isinstance(value, str):
        raise _malformed_token_response()
    normalized = value.strip()
    if normalized == "":
        raise _malformed_token_response()
    return normalized


def _malformed_token_response() -> GmailOAuthError:
    """构造令牌响应畸形的错误。"""
    return GmailOAuthError("GOOGLE_TOKEN_EXCHANGE_FAILED", "Google 授权响应无效，请重新授权")


def _unverified_identity() -> GmailOAuthError:
    """构造身份未验证的错误。"""
    return GmailOAuthError("GOOGLE_IDENTITY_UNVERIFIED", "Google 邮箱身份尚未验证")


# ---------------------------------------------------------------------------
# HTTP 编排
#
# 传输层以**可注入的回调**给出：这样整条链路可以在没有网络的情况下完整测试，
# 而真实实现只是把 httpx 接上去。
# ---------------------------------------------------------------------------


def exchange_google_authorization_code(
    *,
    authorization_code: str,
    code_verifier: str,
    client_id: str,
    redirect_uri: str,
    client_secret: str | None = None,
    now: datetime,
    post_form: Any,
    get_userinfo: Any,
) -> dict[str, Any]:
    """用 PKCE 授权码建立 Google 离线授权并读取已验证身份。

    @param authorization_code: Google 授权码。
    @param code_verifier: PKCE verifier。
    @param client_id: OAuth client ID。
    @param redirect_uri: 回调地址。
    @param client_secret: 可选密钥。
    @param now: 服务端可信时间。
    @param post_form: ``(url, form, timeout_seconds) -> payload``；失败时抛 ``GoogleEndpointFailure``。
    @param get_userinfo: ``(url, headers, timeout_seconds) -> payload``；失败时抛同一安全异常。
    @returns ``{"accessToken", "accessTokenExpiresAt", "identity", "grant"}``。
    @raises GmailOAuthError: 授权失败、响应畸形或身份未验证。
    """
    form = build_token_exchange_form(
        authorization_code=authorization_code, code_verifier=code_verifier,
        client_id=client_id, redirect_uri=redirect_uri, client_secret=client_secret,
    )
    token = _request_google_token(form, post_form)
    subject, email = _request_verified_identity(
        token.access_token, get_userinfo
    )
    # 作用域校验失败会抛 GMAIL_SCOPE_MISSING——授权没给只读权限就无法工作。
    scopes = assert_gmail_readonly_scope(token.granted_scope_text)
    expires_at = calculate_access_token_expiry(now, token.expires_in_seconds)
    if expires_at is None:
        raise _malformed_token_response()

    return {
        "accessToken": token.access_token,
        "accessTokenExpiresAt": expires_at,
        "identity": {"subject": subject, "email": email, "emailVerified": True},
        "grant": {
            "refreshToken": token.refresh_token,
            "grantedScopeText": " ".join(scopes),
        },
    }


def refresh_google_access_token(
    *,
    refresh_token: str,
    client_id: str,
    granted_scope_text: str,
    client_secret: str | None = None,
    now: datetime,
    post_form: Any,
) -> dict[str, Any]:
    """刷新 access token。

    **400 / 401 / 403 表示授权已失效**（Google 用 400 ``invalid_grant`` 表达
    refresh token 被撤销），此时报 ``GMAIL_AUTH_REVOKED`` 让用户重新授权；
    其余失败报 ``GMAIL_CONNECTION_ERROR``。

    @returns ``{"accessToken", "accessTokenExpiresAt", "grantedScopeText"}``。
    @raises GmailSyncError: 授权失效或暂时性故障。
    """
    from ...domain.gmail.sync_plan import GmailSyncError

    form = build_token_refresh_form(
        refresh_token=refresh_token, client_id=client_id, client_secret=client_secret
    )
    # 先校验存储的作用域：连作用域都不对时，刷新成功也没有意义。
    assert_gmail_readonly_scope(granted_scope_text)

    try:
        payload = post_form(
            GOOGLE_TOKEN_ENDPOINT, form, GOOGLE_OAUTH_TIMEOUT_MILLISECONDS / 1000
        )
    except GoogleEndpointFailure as failure:
        if token_endpoint_failure_code(failure.status) == "GMAIL_AUTH_REVOKED":
            raise GmailSyncError(
                "GMAIL_AUTH_REVOKED", "Gmail 授权已失效，需要重新授权"
            ) from failure
        raise GmailSyncError(
            "GMAIL_CONNECTION_ERROR", "无法刷新 Gmail 授权，请稍后重试",
            retryable=failure.retryable,
        ) from failure

    token = parse_google_refresh_response(payload)
    expires_at = calculate_access_token_expiry(now, token.expires_in_seconds)
    if expires_at is None:
        raise _malformed_token_response()
    # OAuth refresh 在 scope 未变化时可省略该字段；此时以已存储且验证过的 grant 为准。
    granted_scopes = assert_gmail_readonly_scope(
        granted_scope_text if token.granted_scope_text is None else token.granted_scope_text
    )
    return {
        "accessToken": token.access_token,
        "accessTokenExpiresAt": expires_at,
        "grantedScopeText": " ".join(granted_scopes),
    }


def revoke_gmail_connection_authorization(
    connection: object,
    cipher: object,
    *,
    post_form: Any,
) -> None:
    """向 Google 撤销连接的授权。

    **撤销失败必须上报**：静默成功会让用户以为凭据已作废，而 Google 侧仍然有效。

    @param connection: 连接记录。
    @param cipher: 加密器。
    @param post_form: 传输层。
    @raises GmailSyncError: 无法完成撤销。
    """
    from ...domain.gmail.sync_plan import GmailSyncError

    refresh_token = decrypt_connection_refresh_token(connection, cipher)
    try:
        post_form(
            GOOGLE_TOKEN_REVOCATION_ENDPOINT, {"token": refresh_token},
            GOOGLE_OAUTH_TIMEOUT_MILLISECONDS / 1000,
        )
    except GoogleEndpointFailure as failure:
        raise GmailSyncError(
            "GMAIL_CONNECTION_ERROR", "Google 未确认 Gmail 授权撤销，请稍后重试",
            retryable=True,
        ) from failure


def _request_google_token(form: dict[str, str], post_form: Any) -> GoogleTokenResponse:
    """请求令牌端点并解析响应。"""
    try:
        payload = post_form(
            GOOGLE_TOKEN_ENDPOINT, form, GOOGLE_OAUTH_TIMEOUT_MILLISECONDS / 1000
        )
    except GoogleEndpointFailure as failure:
        if failure.status is None:
            raise GmailOAuthError(
                "GOOGLE_TOKEN_EXCHANGE_FAILED",
                "无法连接 Google，请稍后重试",
                retryable=failure.retryable,
            ) from failure
        raise GmailOAuthError(
            "GOOGLE_TOKEN_EXCHANGE_FAILED",
            "无法完成 Google 授权，请重新授权或稍后重试",
            retryable=failure.retryable,
        ) from failure
    return parse_google_token_response(payload)


def _request_verified_identity(access_token: str, get_userinfo: Any) -> tuple[str, str]:
    """读取并校验 Google 账户身份。"""
    try:
        payload = get_userinfo(
            GOOGLE_USERINFO_ENDPOINT,
            {"authorization": f"Bearer {access_token}"},
            GOOGLE_OAUTH_TIMEOUT_MILLISECONDS / 1000,
        )
    except GoogleEndpointFailure as failure:
        if failure.status is None:
            raise GmailOAuthError(
                "GOOGLE_TOKEN_EXCHANGE_FAILED", "无法验证 Google 账户身份，请稍后重试",
                retryable=True,
            ) from failure
        raise google_endpoint_error("userinfo", failure.status) from failure
    return parse_verified_identity(payload)
