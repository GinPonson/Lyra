"""Google OIDC 登录来源。

镜像 TypeScript 版 ``google-login-client.ts``。与普通 OAuth 不同，Google 走
**OIDC**：真正的身份来自 ID token，而不是用户信息端点。

因此这里有一条不可替代的分工：

- **签名与时效由官方库验证**（``google-auth``），它负责拉取 Google JWKS 并校验
  `iss` / `aud` / `exp`；
- **本模块只做官方库不覆盖的边界校验**：`nonce` 绑定、`email_verified`、
  账户状态与 owner 准入。

**不得用 JSON 解码替代官方验签入口**——那等于接受任何人伪造的身份令牌。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlencode

from ...application.app_error import ApplicationError

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_LOGIN_CALLBACK_PATH = "/v1/auth/google/callback"
GOOGLE_LOGIN_SCOPES: tuple[str, ...] = ("openid", "email", "profile")

# 官方验签入口的超时；与普通 OAuth 保持一致。
EXCHANGE_TIMEOUT_SECONDS = 15.0

_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SUBJECT = re.compile(r"^[\x21-\x7e]{1,255}$")
_HASH = re.compile(r"^[a-f\d]{64}$")
_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


@dataclass(frozen=True)
class GoogleLoginConfig:
    """登录与 Gmail 离线授权使用不同配置；登录**不申请** Gmail 或 offline 权限。"""

    client_id: str
    client_secret: str
    redirect_uri: str
    owner_email: str | None
    owner_subject: str | None


@dataclass(frozen=True)
class GoogleLoginIdentity:
    """只接受经过 Google 签名与 OIDC 声明校验的身份；不持久化 Google 令牌。"""

    provider: Literal["google"]
    subject: str
    email: str
    display_name: str | None


def load_google_login_config(
    env: Mapping[str, str | None],
) -> GoogleLoginConfig | None:
    """从环境读取并校验 Google 登录配置。

    @param env: 环境变量映射。
    @returns 已校验配置；五项全空时返回 ``None``（来源未启用）。
    @raises ValueError: 部分配置、缺少 owner 或取值非法。
    """
    names = (
        "LYRA_LOGIN_GOOGLE_CLIENT_ID",
        "LYRA_LOGIN_GOOGLE_CLIENT_SECRET",
        "LYRA_LOGIN_GOOGLE_REDIRECT_URI",
        "LYRA_OWNER_EMAIL",
        "LYRA_OWNER_GOOGLE_SUBJECT",
    )
    if all((env.get(name) or "").strip() == "" for name in names):
        return None

    def required(name: str) -> str:
        value = (env.get(name) or "").strip()
        if not value:
            raise ValueError(f"Google 登录配置缺少 {name}")
        return value

    config = GoogleLoginConfig(
        client_id=required("LYRA_LOGIN_GOOGLE_CLIENT_ID"),
        client_secret=required("LYRA_LOGIN_GOOGLE_CLIENT_SECRET"),
        redirect_uri=required("LYRA_LOGIN_GOOGLE_REDIRECT_URI"),
        owner_email=(env.get("LYRA_OWNER_EMAIL") or "").strip().lower() or None,
        owner_subject=(env.get("LYRA_OWNER_GOOGLE_SUBJECT") or "").strip() or None,
    )
    validate_google_login_config(config)
    return config


def validate_google_login_config(config: GoogleLoginConfig) -> None:
    """校验 Google 登录配置。

    @raises ValueError: 凭据、回调地址或 owner 不合法。
    """
    if not config.client_id.strip() or not config.client_secret.strip():
        raise ValueError("Google 登录必须配置 Web OAuth client ID 与 client secret")

    from urllib.parse import urlsplit

    parts = urlsplit(config.redirect_uri)
    hostname = (parts.hostname or "").lower()
    is_local = hostname in _LOCAL_HOSTNAMES
    if (
        not (parts.scheme == "https" or (parts.scheme == "http" and is_local))
        or parts.path != GOOGLE_LOGIN_CALLBACK_PATH
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            f"Google 登录回调必须为固定 HTTPS {GOOGLE_LOGIN_CALLBACK_PATH} 地址"
            "（仅本机开发允许 HTTP）"
        )

    if config.owner_email is None and config.owner_subject is None:
        raise ValueError("必须配置 LYRA_OWNER_EMAIL 或 LYRA_OWNER_GOOGLE_SUBJECT，Lyra 不开放注册")
    if config.owner_email is not None and not _EMAIL.fullmatch(config.owner_email):
        raise ValueError("LYRA_OWNER_EMAIL 格式无效")
    if config.owner_subject is not None and not _SUBJECT.fullmatch(config.owner_subject):
        raise ValueError("LYRA_OWNER_GOOGLE_SUBJECT 格式无效")


def sha256(value: str) -> str:
    """生成 UTF-8 字节的 SHA-256 十六进制文本。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def matches_hash(value: str, expected_hash: str) -> bool:
    """常量时间比较 ``value`` 的 SHA-256 与给定哈希。

    @param value: 待比较的原文（例如 ID token 中的 nonce）。
    @param expected_hash: 期望的 64 位十六进制哈希。
    @returns 是否匹配；哈希格式非法一律视为不匹配。
    """
    if not _HASH.fullmatch(expected_hash):
        return False
    return hmac.compare_digest(sha256(value), expected_hash)


def validate_google_login_claims(
    claims: object,
    *,
    audience: str,
    nonce_hash: str,
    now: datetime,
) -> GoogleLoginIdentity:
    """校验 OIDC 声明并返回身份。

    **只用于签名已经通过的 payload**：本函数不做签名验证，调用方必须先经官方
    验签入口。用 JSON 解码替代验签等于接受任何人伪造的身份。

    @param claims: 已验证签名的 ID token payload。
    @param audience: 期望的 audience（即 client ID）。
    @param nonce_hash: 登录事务 nonce 的 SHA-256。
    @param now: 服务端可信时间。
    @returns 已校验的 Google 身份。
    @raises ApplicationError: 任一声明不满足要求。
    """
    now_seconds = now.timestamp()
    if not isinstance(claims, dict):
        raise _login_failed()

    issuer = claims.get("iss")
    audience_claim = claims.get("aud")
    azp = claims.get("azp")
    expires = claims.get("exp")
    issued_at = claims.get("iat")
    nonce = claims.get("nonce")
    subject = claims.get("sub")
    email = claims.get("email")

    if (
        issuer not in _ISSUERS
        or audience_claim != audience
        or (azp is not None and azp != audience)
        or not _is_int(expires)
        or expires <= now_seconds
        or not _is_int(issued_at)
        or issued_at > now_seconds + 60
        or expires <= issued_at
        or not isinstance(nonce, str)
        or not matches_hash(nonce, nonce_hash)
        or not isinstance(subject, str)
        or not _SUBJECT.fullmatch(subject)
        or claims.get("email_verified") is not True
        or not isinstance(email, str)
        or not _EMAIL.fullmatch(email)
    ):
        raise _login_failed()

    name = claims.get("name")
    return GoogleLoginIdentity(
        provider="google",
        subject=subject,
        email=email.strip().lower(),
        display_name=name.strip()[:256] if isinstance(name, str) and name.strip() else None,
    )


def assert_configured_owner(identity: GoogleLoginIdentity, config: GoogleLoginConfig) -> None:
    """校验身份是否属于配置的 owner。

    邮箱与 subject **各自独立生效**：凡是配置了的维度都必须匹配。
    subject 比较区分大小写，邮箱按已规范化的形式比较。

    @raises ApplicationError: 身份不属于配置的 owner。
    """
    if (
        (config.owner_subject is not None and identity.subject != config.owner_subject)
        or (config.owner_email is not None and identity.email != config.owner_email.lower())
    ):
        raise ApplicationError(
            code="LOGIN_DENIED",
            message="此 Google 账户无权访问这台 Lyra 服务",
            status_code=403,
            retryable=False,
        )


class GoogleLoginClient:
    """Google OIDC 登录客户端。

    @param config: 已校验的 Google 登录配置。
    @param fetcher: 可注入的 HTTP 抓取器（用于 token 端点）。
    @param verify_id_token: 可注入的官方验签入口；默认使用 ``google-auth``。
    """

    def __init__(
        self,
        config: GoogleLoginConfig,
        fetcher: Callable[..., Any] | None = None,
        verify_id_token: Callable[[str], object] | None = None,
    ) -> None:
        validate_google_login_config(config)
        self._config = config
        self._fetch = fetcher
        self._verify_id_token = verify_id_token

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        """构造 Google 授权跳转地址。

        始终携带 `nonce` 与 S256 PKCE；`prompt=select_account` 强制选择账号，
        避免在多账号浏览器里静默复用上一次会话。
        """
        params = {
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_LOGIN_SCOPES),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        nonce_hash: str,
        now: datetime,
    ) -> GoogleLoginIdentity:
        """用授权码交换并用官方库验签 ID token。

        @param code: 上游返回的授权码。
        @param code_verifier: App 侧 PKCE verifier。
        @param nonce_hash: 登录事务 nonce 的 SHA-256。
        @param now: 服务端可信时间。
        @returns 已校验的 Google 身份。
        @raises ApplicationError: 任何一步失败都折叠为同一个安全错误。
        """
        from .oauth_login_client import OAuthHttpResponse, read_bounded_json

        try:
            form = {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "redirect_uri": self._config.redirect_uri,
            }
            response = self._send(
                TOKEN_ENDPOINT,
                headers={"content-type": "application/x-www-form-urlencoded"},
                body=urlencode(form).encode("ascii"),
            )
            if not 200 <= response.status_code < 300:
                raise _login_failed()
            body = read_bounded_json(
                OAuthHttpResponse(
                    status_code=response.status_code,
                    headers=response.headers,
                    body=response.body,
                    redirected=response.redirected,
                )
            )
            if not isinstance(body, dict) or not isinstance(body.get("id_token"), str) or not body["id_token"]:
                raise _login_failed()
            claims = self._verify(body["id_token"])
            return validate_google_login_claims(
                claims,
                audience=self._config.client_id,
                nonce_hash=nonce_hash,
                now=now,
            )
        except ApplicationError:
            raise
        except Exception as error:
            raise _login_failed() from error

    def _send(self, url: str, *, headers: Mapping[str, str], body: bytes) -> Any:
        """调用抓取器；未注入时使用内置 httpx 实现。"""
        if self._fetch is not None:
            return self._fetch(
                url, method="POST", headers=headers, body=body,
                timeout_seconds=EXCHANGE_TIMEOUT_SECONDS,
            )
        from .oauth_login_client import default_fetcher

        return default_fetcher(
            url, method="POST", headers=headers, body=body,
            timeout_seconds=EXCHANGE_TIMEOUT_SECONDS,
        )

    def _verify(self, id_token: str) -> object:
        """用官方验签入口校验 ID token。"""
        if self._verify_id_token is not None:
            return self._verify_id_token(id_token)
        return _default_verify_id_token(id_token, self._config.client_id)


def _default_verify_id_token(id_token: str, audience: str) -> object:
    """使用 ``google-auth`` 校验签名、issuer、audience 与时效。

    @param id_token: 待校验的 ID token。
    @param audience: 期望的 audience（client ID）。
    @returns 已验签的 payload。
    @raises ValueError: 未安装 google-auth。
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token as google_id_token
    except ImportError as error:  # pragma: no cover - 依赖缺失属于部署错误
        raise ValueError("缺少 google-auth，无法验证 Google ID token") from error

    return google_id_token.verify_oauth2_token(id_token, Request(), audience)


def _is_int(value: object) -> bool:
    """判断是否为非布尔整数。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _login_failed() -> ApplicationError:
    """构造统一的、不含任何凭据信息的登录失败错误。"""
    return ApplicationError(
        code="LOGIN_FAILED",
        message="无法验证 Google 登录，请重新登录",
        status_code=401,
        retryable=False,
    )
