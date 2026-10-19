"""登录事务测试。

事务是「无凭据入口 + 一次性凭据发放」的结合点，因此重点覆盖：

1. **PKCE 正确性**——用 RFC 7636 官方测试向量验证，而不是自证；
2. **state 绑定**——未登记或畸形的 state 一律失败关闭；
3. **兑换必须带 App verifier**——只有 loginId 或浏览器 URL 都不足以领取凭据；
4. **容量与限流**——滥用者不得让所有者永久无法登录。

**已删除的覆盖**：``ConfiguredLoginProvider.assert_owner`` 这一单 owner 白名单闸门已
被多用户改造移除，身份到用户的解析改在设备签发用例里按 ``(provider, subject)``
自动注册（契约 §3.10）。因此这里不再构造"拒绝 owner"的来源；原先借它触发的
「失败事务必须被记录、兑换时重放同一错误」仍由 ``access_denied``（用户取消授权）
这一仍在产路径覆盖。

**适配点**：登录响应中的 ``user`` 现在是解析出的 ``UserRecord``，来源身份挂在
``identity`` 上（契约 §3.9），设备签发用例负责一并返回二者。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.login_service import (
    LOGIN_TTL_MILLISECONDS,
    MAX_STARTS_PER_SOURCE_WINDOW,
    LoginStartInput,
    UserLoginService,
    pkce_challenge,
    sha256_text,
)
from lyra.infrastructure.auth.login_provider import (
    ConfiguredLoginProvider,
    LoginIdentity,
)
from lyra.infrastructure.db.user_repository import (
    UserIdentityRecord,
    UserRecord,
)

FIXTURES = Path(__file__).parent / "fixtures" / "device-keys.json"
PUBLIC_KEY_PEM = json.loads(FIXTURES.read_text(encoding="utf-8"))["cases"][0]["inputPem"]

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
CODE_CHALLENGE = pkce_challenge("v" * 43)

# 设备签发用例按契约 §3.9 一并返回解析出的用户与来源身份；登录响应因此可以直接
# 透出与 ``GET /v1/me`` 同形的 ``user``。此处用真实记录类型而不是鸭子类型替身。
STAMP = "2026-10-12T09:00:00.000Z"
RESOLVED_USER = UserRecord(
    id="11111111-1111-1111-1111-111111111111",
    display_name="octocat",
    status="active",
    role="member",
    created_at=STAMP,
    updated_at=STAMP,
)
RESOLVED_IDENTITY = UserIdentityRecord(
    id="22222222-2222-2222-2222-222222222222",
    user_id=RESOLVED_USER.id,
    provider="github",
    subject="12345",
    email=None,
    display_name="octocat",
    created_at=STAMP,
    updated_at=STAMP,
)


class FakeProviderClient:
    """固定返回预设身份的来源客户端。"""

    def __init__(self, identity: LoginIdentity, *, fail: bool = False) -> None:
        self.identity = identity
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        self.calls.append({"state": state, "nonce": nonce, "code_challenge": code_challenge})
        return f"https://provider.example/authorize?state={state}"

    def exchange_code(self, **kwargs: Any) -> LoginIdentity:
        self.calls.append(kwargs)
        if self.fail:
            raise ApplicationError(
                code="LOGIN_FAILED", message="上游失败", status_code=401, retryable=False
            )
        return self.identity


def _provider(identity: LoginIdentity | None = None, *, fail: bool = False):
    """构造一个来源；``fail`` 让它模拟上游交换失败。

    单 owner 白名单的 ``assert_owner`` 钩子已随多用户改造移除（契约 §3.10），
    来源不再持有任何准入判定，身份到用户的映射发生在设备签发用例里。
    """
    resolved = identity or LoginIdentity(
        provider="github", subject="12345", email=None, display_name="octocat"
    )

    client = FakeProviderClient(resolved, fail=fail)
    return ConfiguredLoginProvider(id="github", name="GitHub", client=client)


def _service(*, now: datetime = NOW, **provider_kwargs: Any) -> UserLoginService:
    """构造只带 GitHub 来源的服务，并注入固定时钟与设备签发记录器。"""
    issued: list[dict[str, Any]] = []

    async def authorize(**kwargs: Any) -> dict[str, Any]:
        # 设备签发是异步的：它需要单写队列，而队列用 asyncio.Lock 串行化。
        # ``user`` / ``identity`` 与设备字段一并返回：登录响应必须给出与
        # ``GET /v1/me`` 同形的 user 对象（契约 §3.9）。
        issued.append(kwargs)
        return {
            "deviceId": "device-1",
            "accessToken": "at",
            "refreshToken": "rt",
            "user": RESOLVED_USER,
            "identity": RESOLVED_IDENTITY,
        }

    service = UserLoginService(
        [_provider(**provider_kwargs)], authorize, now=lambda: _service.clock
    )
    service.issued = issued  # type: ignore[attr-defined]
    _service.clock = now  # type: ignore[attr-defined]
    return service


def _start_payload(**overrides: Any) -> LoginStartInput:
    """构造合法的开始登录参数。"""
    base: dict[str, Any] = {
        "display_name": "Pixel 9",
        "public_key_pem": PUBLIC_KEY_PEM,
        "code_challenge": CODE_CHALLENGE,
        "code_challenge_method": "S256",
    }
    base.update(overrides)
    return LoginStartInput(**base)


def _exchange(service: UserLoginService, *args, **kwargs):
    """同步调用异步的兑换方法（测试用）。"""
    return asyncio.run(UserLoginService.exchange(service, *args, **kwargs))


def _advance(minutes: float) -> None:
    """推进测试时钟。"""
    _service.clock = _service.clock + timedelta(minutes=minutes)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# PKCE
# --------------------------------------------------------------------------


def test_pkce_matches_rfc7636_test_vector() -> None:
    """PKCE S256 必须与 RFC 7636 附录 B 的官方向量一致。"""
    assert pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_pkce_output_is_unpadded_base64url() -> None:
    """挑战必须是无填充 base64url，长度 43。"""
    challenge = pkce_challenge("x" * 50)
    assert len(challenge) == 43
    assert "=" not in challenge


# --------------------------------------------------------------------------
# 目录与开始
# --------------------------------------------------------------------------


def test_catalog_lists_all_providers_in_fixed_order() -> None:
    """目录必须按固定顺序列出全部来源，并标记可用性。"""
    catalog = _service().catalog()

    assert [entry["id"] for entry in catalog["providers"]] == ["google", "linuxdo", "github"]
    assert [entry["name"] for entry in catalog["providers"]] == ["Google", "LINUX DO", "GitHub"]
    assert [entry["available"] for entry in catalog["providers"]] == [False, False, True]


def test_start_returns_authorization_url_and_expiry() -> None:
    """成功创建事务应返回授权地址与 TTL 内的过期时间。"""
    response = _service().start("github", _start_payload())

    assert response.login_id
    assert response.authorization_url.startswith("https://provider.example/authorize?")
    assert response.poll_interval_seconds == 2
    assert response.expires_at == "2026-10-12T09:10:00.000Z"


def test_start_rejects_unconfigured_provider() -> None:
    """未配置来源返回 503，而不是 404 或静默失败。"""
    with pytest.raises(ApplicationError) as raised:
        _service().start("google", _start_payload())
    assert raised.value.code == "AUTH_NOT_CONFIGURED"
    assert raised.value.status_code == 503


@pytest.mark.parametrize(
    "overrides",
    [
        {"code_challenge_method": "plain"},
        {"code_challenge": "short"},
        {"code_challenge": "x" * 44},
        {"display_name": "   "},
        {"display_name": "x" * 65},
    ],
)
def test_start_rejects_invalid_arguments(overrides: dict[str, Any]) -> None:
    """参数不合法返回 400。"""
    with pytest.raises(ApplicationError) as raised:
        _service().start("github", _start_payload(**overrides))
    assert raised.value.code == "INVALID_ARGUMENT"


def test_start_rejects_oversized_public_key() -> None:
    """超长公钥在解析前就被拒绝。"""
    with pytest.raises(ApplicationError) as raised:
        _service().start("github", _start_payload(public_key_pem="x" * 4097))
    assert raised.value.code == "INVALID_ARGUMENT"


def test_start_rejects_malformed_public_key() -> None:
    """非公钥文本返回设备公钥错误。"""
    with pytest.raises(ApplicationError) as raised:
        _service().start("github", _start_payload(public_key_pem="-----BEGIN PUBLIC KEY-----\nz\n-----END PUBLIC KEY-----\n"))
    assert raised.value.code == "DEVICE_PUBLIC_KEY_INVALID"


def test_start_rate_limits_single_source() -> None:
    """同一来源在窗口内超过上限返回 429。"""
    service = _service()
    for _ in range(MAX_STARTS_PER_SOURCE_WINDOW):
        service.start("github", _start_payload(), source_key="1.2.3.4")

    with pytest.raises(ApplicationError) as raised:
        service.start("github", _start_payload(), source_key="1.2.3.4")
    assert raised.value.code == "LOGIN_RATE_LIMITED"
    assert raised.value.status_code == 429
    assert raised.value.retryable is True

    # 换一个来源不受影响。
    service.start("github", _start_payload(), source_key="5.6.7.8")


def test_start_rate_limit_window_slides() -> None:
    """窗口滑过后同来源重新可用。"""
    service = _service()
    for _ in range(MAX_STARTS_PER_SOURCE_WINDOW):
        service.start("github", _start_payload(), source_key="1.2.3.4")

    _advance(1.1)
    service.start("github", _start_payload(), source_key="1.2.3.4")


def test_capacity_eviction_prefers_oldest_not_rejecting_newest() -> None:
    """容量占满时淘汰最早事务，**最新请求必须仍能成功**。

    若改为对所有人返回 429，未认证攻击者只要占满槽位就能让所有者永久无法登录。
    """
    service = _service()
    service.start("github", _start_payload())  # 最早的事务，应被淘汰
    for _ in range(200):
        latest = service.start("github", _start_payload())

    # 最早的事务已被淘汰，最新的仍在使用。
    with pytest.raises(ApplicationError) as raised:
        service.complete_callback("github", state="x" * 43)
    assert raised.value.code == "LOGIN_STATE_INVALID"
    assert latest.login_id


# --------------------------------------------------------------------------
# 回调
# --------------------------------------------------------------------------


def _started(**provider_kwargs: Any) -> tuple[UserLoginService, str, str]:
    """创建事务并返回 (服务, loginId, state)，state 从客户端调用记录中取出。"""
    service = _service(**provider_kwargs)
    response = service.start("github", _start_payload())
    provider = service._providers["github"]
    state = provider.client.calls[0]["state"]
    return service, response.login_id, state


def test_callback_completes_and_approves() -> None:
    """合法回调应交换身份并进入 approved。"""
    service, login_id, state = _started()

    service.complete_callback("github", state=state, code="auth-code")

    result = _exchange(service, "github", login_id=login_id, code_verifier="v" * 43)
    assert result["status"] == "complete"
    assert result["deviceId"] == "device-1"
    # user 现在是解析出的 ``UserRecord``，来源身份（含 subject）挂在 identity 上。
    assert result["user"] == RESOLVED_USER
    assert result["identity"].subject == "12345"


def test_callback_rejects_unknown_state() -> None:
    """未登记的 state 必须失败关闭。"""
    service, _, _ = _started()
    with pytest.raises(ApplicationError) as raised:
        service.complete_callback("github", state="A" * 43, code="c")
    assert raised.value.code == "LOGIN_STATE_INVALID"


def test_callback_rejects_malformed_state() -> None:
    """畸形 state 同样失败关闭，不进入哈希查询。"""
    service, _, _ = _started()
    with pytest.raises(ApplicationError) as raised:
        service.complete_callback("github", state="too-short", code="c")
    assert raised.value.code == "LOGIN_STATE_INVALID"


def test_callback_replay_is_rejected() -> None:
    """同一 state 重复回调必须报已处理。"""
    service, _, state = _started()
    service.complete_callback("github", state=state, code="c")

    with pytest.raises(ApplicationError) as raised:
        service.complete_callback("github", state=state, code="c")
    assert raised.value.code == "LOGIN_ALREADY_REDEEMED"
    assert raised.value.status_code == 409


def test_callback_maps_access_denied() -> None:
    """上游 access_denied 必须映射为「登录已取消」。"""
    service, _, state = _started()
    with pytest.raises(ApplicationError) as raised:
        service.complete_callback("github", state=state, error="access_denied")
    assert raised.value.code == "LOGIN_DENIED"


def test_callback_marks_attempt_failed_on_login_denial() -> None:
    """登录被取消时事务必须进入 failed，且兑换返回同一错误。

    适配说明：原用例借已移除的 ``assert_owner`` 白名单制造 ``LOGIN_DENIED``。同一
    错误码在产路径仍由上游 ``access_denied``（用户取消授权）产生，因此改用该触发
    方式，保留「失败事务被记录、兑换时重放同一错误」这条有效覆盖。
    """
    service, login_id, state = _started()
    with pytest.raises(ApplicationError) as raised:
        service.complete_callback("github", state=state, error="access_denied")
    assert raised.value.code == "LOGIN_DENIED"

    with pytest.raises(ApplicationError) as redeemed:
        _exchange(service, "github", login_id=login_id, code_verifier="v" * 43)
    assert redeemed.value.code == "LOGIN_DENIED"


def test_callback_folds_upstream_failure() -> None:
    """上游交换失败折叠为统一登录失败。"""
    service, _, state = _started(fail=True)
    with pytest.raises(ApplicationError) as raised:
        service.complete_callback("github", state=state, code="c")
    assert raised.value.code == "LOGIN_FAILED"


def test_callback_rejects_wrong_provider() -> None:
    """来源不匹配必须拒绝（state 属于 github，却用另一来源回调）。"""
    service, _, state = _started()
    service._providers["linuxdo"] = _provider()
    with pytest.raises(ApplicationError) as raised:
        service.complete_callback("linuxdo", state=state, code="c")
    assert raised.value.code == "LOGIN_FAILED"


def test_expired_attempt_is_rejected() -> None:
    """超过 TTL 的事务必须报过期。"""
    service, login_id, state = _started()
    _advance(LOGIN_TTL_MILLISECONDS / 60000 + 1)

    with pytest.raises(ApplicationError) as raised:
        service.complete_callback("github", state=state, code="c")
    assert raised.value.code == "LOGIN_EXPIRED"

    with pytest.raises(ApplicationError) as redeemed:
        _exchange(service, "github", login_id=login_id, code_verifier="v" * 43)
    assert redeemed.value.code == "LOGIN_EXPIRED"


# --------------------------------------------------------------------------
# 兑换
# --------------------------------------------------------------------------


def test_exchange_requires_app_verifier() -> None:
    """只有 loginId 不足以领取凭据——错误的 verifier 必须拒绝。"""
    service, login_id, state = _started()
    service.complete_callback("github", state=state, code="c")

    with pytest.raises(ApplicationError) as raised:
        _exchange(
            service,
            "github",
            login_id=login_id,
            code_verifier="w" * 43,
            invite_code="some-invite",
        )
    assert raised.value.code == "LOGIN_FAILED"
    assert service.issued == []  # type: ignore[attr-defined]


def test_exchange_rejects_the_wrong_provider_before_invite_authorization() -> None:
    """同一事务使用错误来源兑换时不触发设备或邀请码门禁。"""
    service, login_id, state = _started()
    service._providers["linuxdo"] = ConfiguredLoginProvider(
        id="linuxdo",
        name="LINUX DO",
        client=FakeProviderClient(
            LoginIdentity("linuxdo", "other-provider", None, "other")
        ),
    )
    service.complete_callback("github", state=state, code="c")

    with pytest.raises(ApplicationError) as raised:
        _exchange(
            service,
            "linuxdo",
            login_id=login_id,
            code_verifier="v" * 43,
            invite_code="some-invite",
        )

    assert raised.value.code == "LOGIN_FAILED"
    assert service.issued == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("verifier", ["short", "x" * 42, "x" * 129, "bad chars here"])
def test_exchange_rejects_malformed_verifier(verifier: str) -> None:
    """verifier 格式不合法必须拒绝。"""
    service, login_id, state = _started()
    service.complete_callback("github", state=state, code="c")

    with pytest.raises(ApplicationError) as raised:
        _exchange(service, "github", login_id=login_id, code_verifier=verifier)
    assert raised.value.code == "LOGIN_FAILED"


def test_exchange_polls_pending_before_callback() -> None:
    """回调前轮询返回 pending 而不是错误。"""
    service, login_id, _ = _started()
    assert _exchange(service, "github", login_id=login_id, code_verifier="v" * 43) == {
        "status": "pending"
    }


def test_exchange_is_single_use() -> None:
    """兑换成功后再次兑换必须报已处理，而不是重复签发凭据。"""
    service, login_id, state = _started()
    service.complete_callback("github", state=state, code="c")
    _exchange(service, "github", login_id=login_id, code_verifier="v" * 43)

    with pytest.raises(ApplicationError) as raised:
        _exchange(service, "github", login_id=login_id, code_verifier="v" * 43)
    assert raised.value.code == "LOGIN_ALREADY_REDEEMED"


def test_exchange_never_returns_third_party_tokens() -> None:
    """返回结果只含统一身份字段与设备凭据，不含上游令牌。"""
    service, login_id, state = _started()
    service.complete_callback("github", state=state, code="c")
    result = _exchange(service, "github", login_id=login_id, code_verifier="v" * 43)

    assert set(result) == {
        "status", "deviceId", "accessToken", "refreshToken", "user", "identity",
    }
    # 来源身份原样透出；user 是解析出的账本用户，不再拿 LoginIdentity 冒充（契约 §3.9）。
    assert result["identity"] == RESOLVED_IDENTITY
    assert result["user"] == RESOLVED_USER


def test_exchange_unknown_login_id_reports_expired() -> None:
    """未知 loginId 报过期，不泄漏事务是否存在。"""
    service, _, _ = _started()
    with pytest.raises(ApplicationError) as raised:
        _exchange(service, "github", login_id="unknown", code_verifier="v" * 43)
    assert raised.value.code == "LOGIN_EXPIRED"


def test_state_is_never_stored_in_plaintext() -> None:
    """事务里只能出现 state 哈希，不能出现 state 原文。"""
    service, _, state = _started()
    attempt = next(iter(service._attempts.values()))

    assert attempt.state_hash == sha256_text(state)
    assert state not in repr(attempt)
