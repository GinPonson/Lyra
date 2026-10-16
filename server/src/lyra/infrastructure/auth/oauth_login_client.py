"""普通 OAuth 登录来源（GitHub / LINUX DO）。

镜像 TypeScript 版 ``oauth-login-client.ts``。

三条安全约束：

1. **不把 access token 当成可自行解码的身份声明**——每次交换后都向**固定**用户
   端点验证一次；端点来自服务端常量，不接受任何来源提供的 URL。
2. **不传播底层异常**——授权码、client secret 与第三方令牌都可能出现在异常
   消息或响应正文里，因此一律替换为安全的固定文案。
3. **响应体积有上限**——只信任实际读到的字节数，不信任 ``Content-Length``，
   也不先无上限地解析 JSON。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlencode, urlsplit

from ...application.app_error import ApplicationError
from .login_provider import LoginIdentity

GITHUB_LOGIN_CALLBACK_PATH = "/v1/auth/github/callback"
LINUXDO_LOGIN_CALLBACK_PATH = "/v1/auth/linuxdo/callback"

# 第三方响应体的硬上限；超出即视为异常响应而不是截断处理。
MAX_RESPONSE_BYTES = 64 * 1024
# 一次交换（token + 用户信息）共用的总时限。
EXCHANGE_TIMEOUT_SECONDS = 15.0

_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
_CANONICAL_SUBJECT = re.compile(r"^[1-9]\d{0,15}$")
_ACCESS_TOKEN = re.compile(r"^[A-Za-z\d._~+/-]{1,8192}={0,2}$")
_CONTROL_CHARS = re.compile(r"[\u0000-\u001f\u007f]")
_FORBIDDEN_IN_REDIRECT = re.compile(r"[\u0000-\u0020\u007f?#]")


@dataclass(frozen=True)
class _ProviderDefinition:
    """服务端固定的端点与作用域；不接受任何来源提供的 URL。"""

    name: str
    environment_name: str
    callback_path: str
    authorization_endpoint: str
    token_endpoint: str
    user_endpoint: str
    scope: str


PROVIDERS: dict[str, _ProviderDefinition] = {
    "github": _ProviderDefinition(
        name="GitHub",
        environment_name="GITHUB",
        callback_path=GITHUB_LOGIN_CALLBACK_PATH,
        authorization_endpoint="https://github.com/login/oauth/authorize",
        token_endpoint="https://github.com/login/oauth/access_token",
        user_endpoint="https://api.github.com/user",
        scope="read:user",
    ),
    "linuxdo": _ProviderDefinition(
        name="LINUX DO",
        environment_name="LINUXDO",
        callback_path=LINUXDO_LOGIN_CALLBACK_PATH,
        authorization_endpoint="https://connect.linux.do/oauth2/authorize",
        token_endpoint="https://connect.linux.do/oauth2/token",
        user_endpoint="https://connect.linux.do/api/user",
        scope="user",
    ),
}

OAuthProviderId = Literal["github", "linuxdo"]


@dataclass(frozen=True)
class OAuthLoginConfig:
    """凭据只在服务端配置。

    **不含 owner 白名单**：多用户下登录来源的可用性只看 OAuth 凭据是否齐备，
    谁可以登录由 ``(provider, subject)`` 的用户解析决定（自动注册），而不是由部署
    配置里的一份 subject 名单决定（契约 §3.10）。
    """

    provider: OAuthProviderId
    client_id: str
    client_secret: str
    redirect_uri: str


@dataclass(frozen=True)
class OAuthHttpResponse:
    """已完整读入的第三方响应。

    ``body`` 必须由抓取器按上限读取；本模块在解析前仍会再次核对长度，
    因为"抓取器已经限制过"不能作为跳过校验的理由。
    """

    status_code: int
    headers: Mapping[str, str]
    body: bytes
    redirected: bool = False


class OAuthFetcher(Protocol):
    """可注入的 HTTP 抓取器；生产实现使用 httpx，测试注入固定响应。"""

    def __call__(
        self,
        url: str,
        *,
        method: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> OAuthHttpResponse: ...


def provider_definition(provider: str) -> _ProviderDefinition:
    """返回来源的固定端点定义。

    @raises ValueError: 来源不受支持。
    """
    if provider not in PROVIDERS:
        raise ValueError("不支持此 OAuth 登录来源")
    return PROVIDERS[provider]


def load_oauth_login_config(
    env: Mapping[str, str | None],
    provider: OAuthProviderId,
) -> OAuthLoginConfig | None:
    """从环境读取并校验一种来源的配置。

    完全未配置时返回 ``None`` 关闭该来源；**不接受部分配置**——部分配置会在
    数据库迁移前直接失败，避免服务以"看起来启用了但永远登不进去"的状态启动。

    @param env: 环境变量映射。
    @param provider: 来源 ID。
    @returns 已校验配置，或未配置时的 ``None``。
    @raises ValueError: 部分配置或取值非法。
    """
    definition = provider_definition(provider)
    prefix = f"LYRA_LOGIN_{definition.environment_name}"
    names = (
        f"{prefix}_CLIENT_ID",
        f"{prefix}_CLIENT_SECRET",
        f"{prefix}_REDIRECT_URI",
    )
    if all((env.get(name) or "").strip() == "" for name in names):
        return None

    def required(name: str) -> str:
        value = (env.get(name) or "").strip()
        if not value:
            raise ValueError(f"{definition.name} 登录配置缺少 {name}")
        return value

    config = OAuthLoginConfig(
        provider=provider,
        client_id=required(f"{prefix}_CLIENT_ID"),
        client_secret=required(f"{prefix}_CLIENT_SECRET"),
        redirect_uri=required(f"{prefix}_REDIRECT_URI"),
    )
    validate_oauth_login_config(config)
    return config


def validate_oauth_login_config(config: OAuthLoginConfig) -> None:
    """校验一种来源的完整配置。

    @raises ValueError: 凭据或回调地址不符合约束。
    """
    definition = provider_definition(config.provider)
    for credential in (config.client_id, config.client_secret):
        if (
            not credential.strip()
            or len(credential) > 4096
            or _CONTROL_CHARS.search(credential)
        ):
            raise ValueError(
                f"{definition.name} 登录必须配置有效的 OAuth client ID 与 client secret"
            )

    def invalid_redirect() -> ValueError:
        return ValueError(
            f"{definition.name} 登录回调必须为固定 HTTPS {definition.callback_path} 地址"
            "（仅本机开发允许 HTTP）"
        )

    if _FORBIDDEN_IN_REDIRECT.search(config.redirect_uri):
        raise invalid_redirect()
    parts = urlsplit(config.redirect_uri)
    hostname = (parts.hostname or "").lower()
    is_local = hostname in _LOCAL_HOSTNAMES
    if (
        not (parts.scheme == "https" or (parts.scheme == "http" and is_local))
        or parts.path != definition.callback_path
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise invalid_redirect()


def is_canonical_subject(value: object) -> bool:
    """判断来源主体标识是否是规范的正整数文本（无前导零）。

    供离线管理员初始化入口校验 ``--subject`` 使用；运行时登录不再有 owner 名单。
    """
    if not isinstance(value, str) or not _CANONICAL_SUBJECT.fullmatch(value):
        return False
    # 两侧互转必须一致，从而排除前导零与超出安全整数范围的值。
    return str(int(value)) == value


class OAuthLoginClient:
    """普通 OAuth 登录客户端。

    不将 access token 当作可自行解码的身份声明：每次交换后向固定用户端点验证。
    GitHub 使用 S256 PKCE；LINUX DO 尚无官方 PKCE 支持保证，使用服务端
    confidential client。App 到 Lyra 的独立 PKCE 与 state/事务绑定由登录服务负责。
    """

    def __init__(self, config: OAuthLoginConfig, fetcher: OAuthFetcher | None = None) -> None:
        """@param config: 已校验的来源配置。@param fetcher: 可注入的 HTTP 抓取器。"""
        validate_oauth_login_config(config)
        self._config = config
        self._fetch = fetcher

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        """构造授权跳转地址。

        @param state: 登录事务的 state。
        @param nonce: 登录事务的 nonce（保留参数，普通 OAuth 不使用）。
        @param code_challenge: App 侧 PKCE 挑战；仅 GitHub 会透传给上游。
        @returns 完整授权 URL。
        """
        definition = provider_definition(self._config.provider)
        params = {
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "response_type": "code",
            "scope": definition.scope,
            "state": state,
        }
        if self._config.provider == "github":
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        return f"{definition.authorization_endpoint}?{urlencode(params)}"

    def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        nonce_hash: str,
        now: Any,
    ) -> Any:
        """用授权码交换并验证来源身份。

        @param code: 上游返回的授权码。
        @param code_verifier: App 侧 PKCE verifier；仅 GitHub 会透传。
        @param nonce_hash: 登录事务的 nonce 哈希（保留参数）。
        @param now: 服务端可信时间（保留参数）。
        @returns 已验证的来源身份。
        @raises ApplicationError: 任何一步失败都折叠为同一个安全错误。
        """
        definition = provider_definition(self._config.provider)
        try:
            form = {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "redirect_uri": self._config.redirect_uri,
            }
            if self._config.provider == "github":
                form["code_verifier"] = code_verifier
            token = read_bounded_json(self._send(
                definition.token_endpoint,
                method="POST",
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    "accept": "application/json",
                    "user-agent": "Lyra",
                },
                body=urlencode(form).encode("ascii"),
            ))
            access_token = _require_access_token(token)
            headers = {
                "authorization": f"Bearer {access_token}",
                "accept": (
                    "application/vnd.github+json"
                    if self._config.provider == "github"
                    else "application/json"
                ),
                "user-agent": "Lyra",
            }
            if self._config.provider == "github":
                headers["x-github-api-version"] = "2026-03-10"
            profile = read_bounded_json(self._send(
                definition.user_endpoint, method="GET", headers=headers, body=None
            ))
            return identity_from_profile(profile, self._config.provider)
        except ApplicationError:
            raise
        except Exception as error:
            raise ApplicationError(
                code="LOGIN_FAILED",
                message=f"无法验证 {definition.name} 登录，请重新登录",
                status_code=401,
                retryable=False,
            ) from error

    def _send(
        self,
        url: str,
        *,
        method: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> OAuthHttpResponse:
        """调用抓取器；未注入时使用内置 httpx 实现。"""
        fetcher: OAuthFetcher = self._fetch if self._fetch is not None else default_fetcher
        return fetcher(
            url,
            method=method,
            headers=headers,
            body=body,
            timeout_seconds=EXCHANGE_TIMEOUT_SECONDS,
        )


def _require_access_token(token: object) -> str:
    """从 token 响应中取出可接受的 bearer 令牌。

    @raises ValueError: 响应带有 error 字段，或令牌/类型不符合约束。
    """
    if not isinstance(token, dict) or token.get("error") is not None:
        raise ValueError("Invalid OAuth token response")
    access_token = token.get("access_token")
    token_type = token.get("token_type")
    if (
        not isinstance(access_token, str)
        or not _ACCESS_TOKEN.fullmatch(access_token)
        or not isinstance(token_type, str)
        or token_type.lower() != "bearer"
    ):
        raise ValueError("Invalid OAuth token response")
    return access_token


def read_bounded_json(response: OAuthHttpResponse) -> Any:
    """按真实字节上限读取并解析 JSON 响应。

    @param response: 抓取器返回的响应。
    @returns 解析后的 JSON 值。
    @raises ValueError: 状态非 2xx、发生重定向、声明或实际长度超限、或不是合法 JSON。
    """
    declared = response.headers.get("content-length")
    if (
        not 200 <= response.status_code < 300
        or response.redirected
        or (declared is not None and (not declared.isdigit() or int(declared) > MAX_RESPONSE_BYTES))
        or len(response.body) > MAX_RESPONSE_BYTES
    ):
        raise ValueError("Invalid OAuth HTTP response")
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid OAuth HTTP response") from error


def identity_from_profile(profile: object, provider: OAuthProviderId) -> Any:
    """从用户信息响应中提取来源身份。

    只做身份登录，**不申请也不推断经验证邮箱**，也不把额外 profile 字段透传出去。

    @param profile: 用户信息 JSON。
    @param provider: 来源 ID。
    @returns 已验证的来源身份。
    @raises ValueError: 身份字段或账户状态不合法。
    """

    if (
        not isinstance(profile, dict)
        or profile.get("error") is not None
        or isinstance(profile.get("id"), bool)
        or not isinstance(profile.get("id"), int)
        or profile["id"] <= 0
    ):
        raise ValueError("Invalid OAuth user identity")

    if provider == "linuxdo":
        active = profile.get("active")
        silenced = profile.get("silenced")
        if (
            (active is not None and not isinstance(active, bool))
            or active is False
            or (silenced is not None and not isinstance(silenced, bool))
        ):
            raise ValueError("Invalid OAuth account status")
        # silenced 是论坛禁言状态，不代表第三方登录被禁用；账号是否可用只看上方的
        # 状态字段与不可变 ID，不看昵称或邮箱。

    fallback = profile.get("login") if provider == "github" else profile.get("username")
    return LoginIdentity(
        provider=provider,
        subject=str(profile["id"]),
        email=None,
        display_name=_display_name(profile.get("name")) or _display_name(fallback),
    )


def _display_name(value: object) -> str | None:
    """清洗展示名；空值返回 ``None``，长度上限 256。"""
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()[:256]
    return cleaned or None


def default_fetcher(
    url: str,
    *,
    method: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_seconds: float,
) -> OAuthHttpResponse:
    """基于 httpx 的生产抓取器。

    使用流式读取并按上限截断：**不先无上限地把响应读进内存**，也不跟随重定向
    （跟随即等于把凭据发给第三方控制的地址）。
    """
    import httpx

    with httpx.Client(follow_redirects=False, timeout=timeout_seconds) as client:
        with client.stream(method, url, headers=dict(headers), content=body) as response:
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ValueError("OAuth response is too large")
                chunks.append(chunk)
            return OAuthHttpResponse(
                status_code=response.status_code,
                headers=dict(response.headers),
                body=b"".join(chunks),
            )
