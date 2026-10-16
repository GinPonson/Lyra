"""登录事务编排。

镜像 TypeScript 版 ``owner-login-service.ts``。

每个事务绑定固定来源；**App→Lyra 始终使用独立 PKCE**（App 的 verifier 不进入
浏览器），Google / GitHub 另外使用上游 PKCE。第三方令牌不返回 App；事务一次性
且重启失效。

三条不可放宽的约束：

1. **state 只存哈希**，且回调必须命中已登记事务，否则一律失败关闭；
2. **兑换必须用 App verifier**——只有 loginId 或浏览器 URL 都不足以领取凭据；
3. **先占用再执行**：状态机在首个 await 之前就推进，避免并发兑换产生两个胜者。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..infrastructure.auth.login_provider import (
    LOGIN_PROVIDER_IDS,
    LOGIN_PROVIDER_NAMES,
    ConfiguredLoginProvider,
    LoginIdentity,
)
from ..security.device_auth import normalize_device_public_key
from .app_error import ApplicationError

LOGIN_TTL_MILLISECONDS = 10 * 60 * 1000
# 终态事务短暂保留，使重放得到准确的 409/410，随后立即释放容量。
TERMINAL_RETENTION_MILLISECONDS = 60 * 1000
MAX_LOGIN_ATTEMPTS = 128
# 单个来源的滚动窗口限流；/start 无凭据，必须防止单点占满全局容量。
SOURCE_WINDOW_MILLISECONDS = 60 * 1000
MAX_STARTS_PER_SOURCE_WINDOW = 20

_CODE_CHALLENGE = re.compile(r"^[A-Za-z\d_-]{43}$")
_CODE_VERIFIER = re.compile(r"^[A-Za-z\d._~-]{43,128}$")


@dataclass(frozen=True)
class LoginStartInput:
    """发起登录所需的 App 侧参数。"""

    display_name: str
    public_key_pem: str
    code_challenge: str
    code_challenge_method: str


@dataclass(frozen=True)
class LoginStartResponse:
    """已创建的登录事务。"""

    login_id: str
    authorization_url: str
    expires_at: str
    poll_interval_seconds: int


@dataclass
class LoginAttempt:
    """一次登录事务的内部状态。"""

    id: str
    provider: str
    state_hash: str
    nonce_hash: str
    provider_code_verifier: str | None
    app_code_challenge: str
    public_key_pem: str
    display_name: str
    expires_at: float
    terminated_at: float | None = None
    status: str = "pending"
    identity: LoginIdentity | None = None
    error: ApplicationError | None = None


class UserLoginService:
    """绑定来源、状态与 PKCE 的一次性登录事务。"""

    def __init__(
        self,
        providers: Sequence[ConfiguredLoginProvider],
        device_authorizer: Callable[..., Mapping[str, Any]],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """@param providers: 已装配的来源。@param device_authorizer: 签发设备凭据的用例。"""
        self._providers: dict[str, ConfiguredLoginProvider] = {}
        for provider in providers:
            if provider.id not in LOGIN_PROVIDER_IDS or provider.id in self._providers:
                raise ValueError("登录提供方重复或无效")
            self._providers[provider.id] = provider
        self._devices = device_authorizer
        self._now = now
        self._attempts: dict[str, LoginAttempt] = {}
        self._states: dict[str, str] = {}
        # 三种登录共享来源限流与容量，切换来源不能绕过预算。
        self._source_starts: dict[str, list[float]] = {}

    def catalog(self) -> dict[str, list[dict[str, Any]]]:
        """返回公开的登录来源目录（只含名称与可用性）。"""
        return {
            "providers": [
                {
                    "id": provider_id,
                    "name": LOGIN_PROVIDER_NAMES[provider_id],
                    "available": provider_id in self._providers,
                }
                for provider_id in LOGIN_PROVIDER_IDS
            ]
        }

    def start(
        self,
        provider_id: str,
        start_input: LoginStartInput,
        source_key: str | None = None,
    ) -> LoginStartResponse:
        """创建一次登录事务。

        @param provider_id: 来源 ID。
        @param start_input: App 提供的设备名、公钥与 PKCE 挑战。
        @param source_key: 限流用的来源键。
        @returns 登录 ID、授权地址与过期时间。
        @raises ApplicationError: 来源未配置、参数非法或来源超限。
        """
        # **参数校验先于"来源是否配置"的判断。**
        #
        # 参考实现在 transport 层就用 schema 校验正文，因此**请求是否合法与服务端
        # 是否配置无关**：一个格式错误的请求应当得到 400，而不是"服务端未配置"。
        #
        # 反过来的顺序会让客户端拿到误导性的错误：它以为要等服务端配置，实际上该改
        # 自己的请求。而 `AUTH_NOT_CONFIGURED` 是 503——客户端可能据此重试，永远等不到。
        if (
            start_input.code_challenge_method != "S256"
            or not _CODE_CHALLENGE.fullmatch(start_input.code_challenge)
            or not 1 <= len(start_input.display_name.strip()) <= 64
            or len(start_input.public_key_pem) > 4096
        ):
            raise ApplicationError(
                code="INVALID_ARGUMENT", message="登录参数无效", status_code=400, retryable=False
            )
        provider = self._provider(provider_id)
        try:
            public_key_pem = normalize_device_public_key(start_input.public_key_pem).pem
        except Exception as error:
            raise ApplicationError(
                code="DEVICE_PUBLIC_KEY_INVALID",
                message="设备公钥格式无效",
                status_code=400,
                retryable=False,
            ) from error

        self._prune_expired()
        self._assert_source_budget(source_key)
        # 容量被滥用时淘汰最早的事务，而不是对所有人返回 429：否则未认证的攻击者
        # 只要占满槽位，就能让所有者永久无法发起新登录。
        while len(self._attempts) >= MAX_LOGIN_ATTEMPTS:
            oldest = next(iter(self._attempts), None)
            if oldest is None:
                break
            self._drop_attempt(oldest)

        now = self._now_ms()
        state = _random_base64url(32)
        nonce = _random_base64url(32)
        provider_code_verifier = _random_base64url(48)
        attempt = LoginAttempt(
            id=str(uuid.uuid4()),
            provider=provider_id,
            state_hash=sha256_text(state),
            nonce_hash=sha256_text(nonce),
            provider_code_verifier=provider_code_verifier,
            app_code_challenge=start_input.code_challenge,
            public_key_pem=public_key_pem,
            display_name=start_input.display_name.strip(),
            expires_at=now + LOGIN_TTL_MILLISECONDS,
        )
        authorization_url = provider.client.authorization_url(
            state=state, nonce=nonce, code_challenge=pkce_challenge(provider_code_verifier)
        )
        self._attempts[attempt.id] = attempt
        self._states[attempt.state_hash] = attempt.id
        return LoginStartResponse(
            login_id=attempt.id,
            authorization_url=authorization_url,
            expires_at=_iso(now + LOGIN_TTL_MILLISECONDS),
            poll_interval_seconds=2,
        )

    def complete_callback(
        self,
        provider_id: str,
        *,
        state: str,
        code: str | None = None,
        error: str | None = None,
    ) -> None:
        """浏览器回调：校验 state 并交换授权码，得到**已验证的来源身份**。

        **在首个可选失败点之前就占用状态**，阻止同一授权码被并发消费。

        **这里不创建用户**：回调只证明"浏览器完成了来源验证"。用户是在
        :meth:`exchange` 中、App 用绑定 verifier 兑换凭据时才解析或自动注册的
        ——授权取消、code 无效或事务过期都不会留下一个用户（计划 §4.3）。

        @param provider_id: 来源 ID。
        @param state: 回调携带的 state 原文。
        @param code: 授权码。
        @param error: 上游返回的错误标识。
        @raises ApplicationError: state 无效、事务已用或授权码交换失败。
        """
        provider = self._provider(provider_id)
        login_id = self._states.get(sha256_text(state)) if _CODE_CHALLENGE.fullmatch(state) else None
        if login_id is None:
            raise ApplicationError(
                code="LOGIN_STATE_INVALID",
                message="登录请求已失效，请返回 App 重新登录",
                status_code=400,
                retryable=False,
            )
        attempt = self._require_attempt(login_id, provider_id)
        if attempt.status != "pending":
            raise _already_redeemed()
        attempt.status = "verifying"
        code_verifier = attempt.provider_code_verifier
        attempt.provider_code_verifier = None
        try:
            if error is not None:
                raise _login_denied() if error == "access_denied" else _login_failed()
            if not code or len(code) > 4096 or code_verifier is None:
                raise _login_failed()
            identity = provider.client.exchange_code(
                code=code,
                code_verifier=code_verifier,
                nonce_hash=attempt.nonce_hash,
                now=self._now_dt(),
            )
            self._assert_not_expired(attempt)
            if identity.provider != provider_id or not identity.subject:
                raise _login_failed()
            # 只保留统一身份字段，不透传上游额外资料或第三方令牌。
            attempt.identity = LoginIdentity(
                provider=identity.provider,
                subject=identity.subject,
                email=identity.email,
                display_name=identity.display_name,
            )
            attempt.status = "approved"
        except Exception as error_:
            attempt.status = "failed"
            attempt.terminated_at = self._now_ms()
            attempt.error = error_ if isinstance(error_, ApplicationError) else _login_failed()
            raise attempt.error from error_

    async def exchange(
        self,
        provider_id: str,
        *,
        login_id: str,
        code_verifier: str,
    ) -> dict[str, Any]:
        """轮询与最终兑换；两者都必须带 App verifier。

        **自动注册发生在本方法的成功路径上**：verifier 与 challenge 比对通过、事务处于
        ``approved`` 之后才调用设备签发用例，而后者按已验证身份解析或创建用户。
        未完成验证（pending / failed / 已兑换）时不会创建任何用户。

        @param provider_id: 来源 ID。
        @param login_id: 登录事务 ID。
        @param code_verifier: App 侧 PKCE verifier。
        @returns ``{"status": "pending"}`` 或兑换完成的凭据与用户对象。
        @raises ApplicationError: verifier 不符、事务失败、已用或已过期；
            该身份的用户已停用时返回 ``USER_DISABLED``（403）。
        """
        # **verifier 的格式检查先于"来源是否配置"。**
        #
        # 与 :meth:`start` 同理：请求的**格式**是否合法与服务端配置无关。PKCE 比对
        # 需要 attempt，因此那一步仍在后面——但格式不合法的请求不该拿到 503。
        if not _CODE_VERIFIER.fullmatch(code_verifier):
            raise _login_failed()
        self._provider(provider_id)
        attempt = self._require_attempt(login_id, provider_id)
        if not hmac.compare_digest(pkce_challenge(code_verifier), attempt.app_code_challenge):
            raise _login_failed()
        if attempt.status in ("pending", "verifying"):
            return {"status": "pending"}
        if attempt.status == "failed":
            raise attempt.error if attempt.error is not None else _login_failed()
        if attempt.status != "approved" or attempt.identity is None:
            raise _already_redeemed()

        # 先占用再开始签发凭据；同一事件循环中的并发兑换只能有一个胜者。
        attempt.status = "exchanging"
        identity = attempt.identity
        try:
            credentials = await self._devices(
                identity=identity,
                public_key_pem=attempt.public_key_pem,
                display_name=attempt.display_name,
            )
            attempt.status = "consumed"
            attempt.terminated_at = self._now_ms()
            attempt.identity = None
            # 凭据里已经带上本次解析出的 ``user`` 与 ``identity``（由设备签发用例返回），
            # 因此这里不再自己拼一个 user 字段——两处各拼一次必然会在某个字段上漂移。
            return {"status": "complete", **credentials}
        except Exception as error:
            attempt.status = "failed"
            attempt.terminated_at = self._now_ms()
            attempt.identity = None
            attempt.error = error if isinstance(error, ApplicationError) else _login_failed()
            raise attempt.error from error

    # -- 内部 --------------------------------------------------------------

    def _provider(self, provider_id: str) -> ConfiguredLoginProvider:
        """取出来源；未配置时返回 503。"""
        provider = self._providers.get(provider_id)
        if provider is None:
            raise ApplicationError(
                code="AUTH_NOT_CONFIGURED",
                message="服务端尚未启用此登录方式",
                status_code=503,
                retryable=False,
            )
        return provider

    def _require_attempt(self, login_id: str, provider_id: str) -> LoginAttempt:
        """按 ID 取出未过期事务，并校验来源绑定。"""
        attempt = self._attempts.get(login_id)
        if attempt is None:
            raise _login_expired()
        if attempt.provider != provider_id:
            raise _login_failed()
        self._assert_not_expired(attempt)
        return attempt

    def _assert_not_expired(self, attempt: LoginAttempt) -> None:
        """过期即失败。"""
        if self._now_ms() >= attempt.expires_at:
            raise _login_expired()

    def _prune_expired(self) -> None:
        """清理过期事务与已释放的来源窗口。"""
        now = self._now_ms()
        for attempt in list(self._attempts.values()):
            expired = now >= attempt.expires_at
            # 终态事务短暂保留即可，否则一次正常登录会占用全局容量直到 TTL 结束。
            finished = (
                attempt.terminated_at is not None
                and now >= attempt.terminated_at + TERMINAL_RETENTION_MILLISECONDS
            )
            if expired or finished:
                self._drop_attempt(attempt.id)
        for source, timestamps in list(self._source_starts.items()):
            recent = [at for at in timestamps if now - at < SOURCE_WINDOW_MILLISECONDS]
            if not recent:
                del self._source_starts[source]
            elif len(recent) != len(timestamps):
                self._source_starts[source] = recent

    def _drop_attempt(self, login_id: str) -> None:
        """释放事务，同时移除登录 ID 与 state 哈希两个索引。"""
        attempt = self._attempts.pop(login_id, None)
        if attempt is None:
            return
        self._states.pop(attempt.state_hash, None)

    def _assert_source_budget(self, source_key: str | None) -> None:
        """对无凭据的 /start 端点做单来源滚动窗口限流。"""
        if not source_key:
            return
        now = self._now_ms()
        recent = [at for at in self._source_starts.get(source_key, []) if now - at < SOURCE_WINDOW_MILLISECONDS]
        if len(recent) >= MAX_STARTS_PER_SOURCE_WINDOW:
            raise ApplicationError(
                code="LOGIN_RATE_LIMITED",
                message="登录请求过多，请稍后重试",
                status_code=429,
                retryable=True,
            )
        recent.append(now)
        self._source_starts[source_key] = recent

    def _now_dt(self) -> datetime:
        """当前时间；未注入时钟时使用系统时间。"""
        if self._now is not None:
            return self._now()

        return datetime.now(UTC)

    def _now_ms(self) -> float:
        """当前时间的毫秒表示。"""
        return self._now_dt().timestamp() * 1000


def pkce_challenge(verifier: str) -> str:
    """计算 PKCE S256 挑战：base64url(sha256(verifier))，无填充。

    输入按 **ASCII** 编码，与现有实现一致；PKCE verifier 的字符集本就限定在
    ASCII 可见范围内，用 UTF-8 会在含非 ASCII 输入时算出不同结果。
    """
    digest = hashlib.sha256(verifier.encode("ascii", errors="strict")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def sha256_text(value: str) -> str:
    """UTF-8 字节的 SHA-256 十六进制文本。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _random_base64url(byte_count: int) -> str:
    """生成无填充 base64url 随机值。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(byte_count)).rstrip(b"=").decode("ascii")


def _iso(milliseconds: float) -> str:
    """把毫秒时间戳渲染为 ISO UTC 文本。"""

    return (
        datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _already_redeemed() -> ApplicationError:
    return ApplicationError(
        code="LOGIN_ALREADY_REDEEMED",
        message="此登录请求已经处理，请重新登录",
        status_code=409,
        retryable=False,
    )


def _login_failed() -> ApplicationError:
    return ApplicationError(
        code="LOGIN_FAILED", message="无法完成登录，请重新登录", status_code=401, retryable=False
    )


def _login_denied() -> ApplicationError:
    return ApplicationError(
        code="LOGIN_DENIED", message="登录已取消", status_code=403, retryable=False
    )


def _login_expired() -> ApplicationError:
    return ApplicationError(
        code="LOGIN_EXPIRED", message="登录请求已过期，请重新登录", status_code=410, retryable=False
    )
