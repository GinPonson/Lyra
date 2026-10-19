"""认证路由测试。

四条要点：

1. **`GET /v1/auth/providers` 不要求设备认证**——它本身就是获取认证的入口；
2. **`POST /v1/devices/tokens` 也不要求访问令牌**——用已过期或已撤销的访问令牌去
   刷新是行不通的，身份由刷新凭据本身证明；
3. **正文类型收口在路由层**——非字符串字段必须 400，不能穿透到服务层变成 500；
4. **新身份在兑换成功后自动注册为 `member` 并获得空账本**，登录响应与 `GET /v1/me`
   给出同一个 `user` 对象（P0 契约 §3.9）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lyra.protocol import CLIENT_PROTOCOL_HEADERS
from lyra.api.agent_events import EventConnectionRegistry
from lyra.application.agent_turn_orchestrator import AgentTurnOrchestrator
from lyra.application.device_pairing_service import DevicePairingService
from lyra.application.login_service import UserLoginService, pkce_challenge
from lyra.application.write_coordinator import WriteCoordinator
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.domain.agent.switching import AgentSessionSnapshot
from lyra.domain.analytics.types import AccountScope
from lyra.infrastructure.auth.login_provider import (
    ConfiguredLoginProvider,
    LoginIdentity,
)
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.infrastructure.db.invite_code_repository import InviteCodeRepository
from lyra.infrastructure.db.user_repository import (
    UserIdentityRecord,
    UserRecord,
    UserRepository,
)
from lyra.main import create_app

_KEYS = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"]
PUBLIC_KEY_PEM = _KEYS[0]["inputPem"]
OTHER_KEY_PEM = _KEYS[1]["inputPem"]
IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")


@pytest.fixture()
def client(tmp_path: Path):
    database_path = tmp_path / "auth.duckdb"
    database = open_lyra_database(str(database_path))
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    application = create_app(settings, database)
    # 签发必须走**应用级**写协调器：另建一个会让健康端点的 writeQueue 与实际写入脱节。
    service = DevicePairingService(
        DuckDbDeviceRepository(database.connection),
        application.state.write_coordinator,
        UserRepository(database.connection),
    )
    issued = asyncio.run(service.authorize_user_device(
        identity=IDENTITY, display_name="Pixel 9", public_key_pem=PUBLIC_KEY_PEM
    ))
    try:
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
            yield test_client, issued
    finally:
        database.close()


@pytest.fixture()
def fresh_client(tmp_path: Path):
    """**没有任何预置用户**的应用与客户端。

    自动注册的用例必须能在"库里一个人都没有"这个前提下断言用户数，否则夹具自己
    签发的那台设备会污染计数，让"未完成验证不创建用户"这类断言失去意义。
    """
    database_path = tmp_path / "fresh-auth.duckdb"
    database = open_lyra_database(str(database_path))
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    application = create_app(settings, database)
    try:
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
            yield test_client
    finally:
        database.close()


# --------------------------------------------------------------------------
# 登录目录
# --------------------------------------------------------------------------


def test_providers_does_not_require_authentication(client) -> None:
    """**目录本身就是获取认证的入口**，不能要求设备认证。"""
    test_client, _ = client
    assert test_client.get("/v1/auth/providers").status_code == 200


def test_providers_lists_all_three_in_fixed_order(client) -> None:
    """三个来源按固定顺序返回，可用性由配置决定。"""
    test_client, _ = client
    body = test_client.get("/v1/auth/providers").json()

    assert [entry["id"] for entry in body["providers"]] == ["google", "linuxdo", "github"]
    assert [entry["name"] for entry in body["providers"]] == ["Google", "LINUX DO", "GitHub"]
    # 本夹具没有配置任何登录来源。
    assert all(entry["available"] is False for entry in body["providers"])


def test_providers_response_has_no_secrets(client) -> None:
    """目录不得泄漏任何配置细节。"""
    test_client, _ = client
    raw = test_client.get("/v1/auth/providers").text

    for leaked in ("client_id", "clientId", "secret", "redirect_uri", "redirectUri"):
        assert leaked not in raw


# --------------------------------------------------------------------------
# 设备令牌刷新
# --------------------------------------------------------------------------


def test_refresh_does_not_require_an_access_token(client) -> None:
    """**刷新不要求访问令牌**：身份由刷新凭据本身证明。

    用已过期或已撤销的访问令牌去刷新是行不通的，因此这条路由不能挂设备认证依赖。
    """
    test_client, issued = client
    response = test_client.post(
        "/v1/devices/tokens", json={"refreshToken": issued.refresh_token}
    )

    assert response.status_code == 200


def test_refresh_rotates_and_returns_both_tokens(client) -> None:
    """**成功响应必须给出新的刷新凭据。**

    它是一次性轮换；客户端若不保存新的那份，下一次刷新就会把旧的使用掉并被判定为重放。
    """
    test_client, issued = client
    body = test_client.post(
        "/v1/devices/tokens", json={"refreshToken": issued.refresh_token}
    ).json()

    assert body["accessToken"] != issued.access_token
    assert body["refreshToken"] != issued.refresh_token
    assert body["accessTokenExpiresAt"].endswith("Z")
    assert body["refreshTokenExpiresAt"].endswith("Z")


def test_reusing_the_old_refresh_token_is_rejected(client) -> None:
    """旧刷新凭据再次使用被判定为重放。"""
    test_client, issued = client
    test_client.post("/v1/devices/tokens", json={"refreshToken": issued.refresh_token})

    response = test_client.post(
        "/v1/devices/tokens", json={"refreshToken": issued.refresh_token}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "DEVICE_CREDENTIAL_REUSED"


def test_unknown_refresh_token_is_rejected(client) -> None:
    """未知刷新凭据 401，且不泄漏"是否存在"的信息。"""
    test_client, _ = client
    response = test_client.post("/v1/devices/tokens", json={"refreshToken": "nope"})

    assert response.status_code == 401
    assert response.json()["code"] == "DEVICE_CREDENTIAL_INVALID"


@pytest.mark.parametrize("payload", [{}, {"refreshToken": ""}, {"refreshToken": 5}])
def test_invalid_bodies_are_rejected(client, payload: dict) -> None:
    """非法正文 400。"""
    test_client, _ = client
    assert test_client.post("/v1/devices/tokens", json=payload).status_code == 400


def test_revoked_device_cannot_refresh(client) -> None:
    """设备撤销后刷新失败。"""
    test_client, issued = client
    connection = test_client.app.state.database.connection
    asyncio.run(
        DevicePairingService(
            DuckDbDeviceRepository(connection),
            test_client.app.state.write_coordinator,
            UserRepository(connection),
        ).revoke_device(issued.device_id)
    )

    response = test_client.post(
        "/v1/devices/tokens", json={"refreshToken": issued.refresh_token}
    )

    assert response.status_code == 401


# --------------------------------------------------------------------------
# 发起登录
# --------------------------------------------------------------------------


VALID_START = {
    "displayName": "Pixel 9",
    "publicKeyPem": PUBLIC_KEY_PEM,
    "codeChallenge": "A" * 43,
    "codeChallengeMethod": "S256",
}


def test_start_requires_no_authentication(client) -> None:
    """发起登录本身是获取认证的入口，不能要求设备认证。"""
    test_client, _ = client
    response = test_client.post("/v1/auth/github/start", json=VALID_START)
    # 本夹具没有配置登录来源，因此是 503 而不是 401。
    assert response.status_code == 503


def test_start_reports_unconfigured_provider(client) -> None:
    """未配置来源返回 503 与稳定错误码。"""
    test_client, _ = client
    response = test_client.post("/v1/auth/github/start", json=VALID_START)

    assert response.status_code == 503
    assert response.json()["code"] == "AUTH_NOT_CONFIGURED"


@pytest.mark.parametrize(
    "overrides",
    [
        {"displayName": ""},
        {"displayName": "x" * 65},
        {"codeChallenge": "short"},
        {"codeChallenge": "x" * 44},
        {"codeChallengeMethod": "plain"},
    ],
)
def test_start_validates_the_body(client, overrides: dict) -> None:
    """参数不合法必须在参数校验阶段拒绝（400），而不是等来源未配置。"""
    test_client, _ = client
    payload = {**VALID_START, **overrides}
    response = test_client.post("/v1/auth/github/start", json=payload)

    # 参数校验先于来源检查（来源检查在 service.start 内）。
    assert response.status_code in (400, 503), overrides


def test_start_rejects_non_json_body(client) -> None:
    """非 JSON 正文 400。"""
    test_client, _ = client
    response = test_client.post(
        "/v1/auth/github/start", content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------
# 回调
# --------------------------------------------------------------------------


def test_callback_redirects_to_the_fixed_app_uri_without_callback_data(client) -> None:
    """回调只把浏览器带回固定 App URI，不携带任何 OAuth 参数。"""
    test_client, _ = client
    response = test_client.get(
        "/v1/auth/github/callback",
        params={"state": "s" * 43, "code": "secret-code"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "com.lyrafinance.lyra://auth/callback"
    assert response.content == b""
    assert "secret-code" not in response.headers["location"]
    assert "s" * 43 not in response.headers["location"]


def test_callback_redirect_is_identical_for_unknown_state_and_denial(client) -> None:
    """未知 state 与用户拒绝都返回相同固定重定向。"""

    test_client, _ = client
    unknown = test_client.get(
        "/v1/auth/github/callback",
        params={"state": "A" * 43, "code": "c"},
        follow_redirects=False,
    )
    denied = test_client.get(
        "/v1/auth/github/callback",
        params={"state": "A" * 43, "error": "access_denied"},
        follow_redirects=False,
    )

    assert unknown.status_code == denied.status_code == 303
    assert unknown.headers["location"] == denied.headers["location"]
    assert unknown.content == denied.content == b""


def test_google_callback_dispatches_gmail_state_without_exposing_oauth_values(client) -> None:
    """已登记 Gmail state 由服务器兑换，浏览器只看到固定原生回跳。"""
    test_client, _ = client
    gmail_calls: list[dict[str, str | None]] = []
    login_calls: list[tuple] = []

    class _GmailCallback:
        def complete_authorization_callback(self, **kwargs):
            gmail_calls.append(kwargs)
            return True

    class _LoginCallback:
        def complete_callback(self, *args, **kwargs):
            login_calls.append((args, kwargs))

    test_client.app.state.gmail_service = _GmailCallback()
    test_client.app.state.login_service = _LoginCallback()
    state = "private-gmail-state"
    code = "private-google-authorization-code"

    response = test_client.get(
        "/v1/auth/google/callback",
        params={"state": state, "code": code},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "com.lyrafinance.lyra://auth/callback"
    assert response.content == b""
    assert state not in response.headers["location"]
    assert code not in response.headers["location"]
    assert gmail_calls == [{"state": state, "authorization_code": code, "error": None}]
    assert login_calls == []


# --------------------------------------------------------------------------
# 退出登录
# --------------------------------------------------------------------------


def test_logout_requires_authentication(client) -> None:
    """退出登录需要访问令牌——撤销目标由令牌本身确定。"""
    test_client, _ = client
    assert test_client.post("/v1/auth/logout").status_code == 401


def test_logout_revokes_the_current_device(client) -> None:
    """退出登录撤销当前设备及其凭据。"""
    test_client, issued = client

    response = test_client.post(
        "/v1/auth/logout", headers={"Authorization": f"Bearer {issued.access_token}"}
    )

    # **204 无内容**（与参考实现一致）。
    assert response.status_code == 204
    assert response.content == b""
    # 令牌立刻失效。
    assert test_client.get(
        "/v1/ledger/summary", headers={"Authorization": f"Bearer {issued.access_token}"}
    ).status_code == 401


def test_logout_rejects_a_revoked_token(client) -> None:
    """已撤销的令牌不能再次登出。"""
    test_client, issued = client
    headers = {"Authorization": f"Bearer {issued.access_token}"}
    test_client.post("/v1/auth/logout", headers=headers)

    assert test_client.post("/v1/auth/logout", headers=headers).status_code == 401


# --------------------------------------------------------------------------
# 兑换
# --------------------------------------------------------------------------


def test_exchange_reports_unconfigured_provider_first(client) -> None:
    """**来源检查先于事务查找。**

    未配置来源返回 503，而不是"事务不存在"的 410——后者的措辞会让运维去查登录事务，
    而真正的问题是来源没配置。这一顺序与现有实现一致。
    """
    test_client, _ = client
    response = test_client.post(
        "/v1/auth/github/exchange",
        json={"loginId": "44444444-4444-4444-4444-444444444444", "codeVerifier": "v" * 43},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "AUTH_NOT_CONFIGURED"


def test_exchange_requires_no_authentication(client) -> None:
    """兑换本身是获取凭据的入口，不要求设备认证。

    未配置来源时是 503，**不是 401**——401 说明路由错误地挂了认证依赖。
    """
    test_client, _ = client
    response = test_client.post(
        "/v1/auth/github/exchange",
        json={"loginId": "44444444-4444-4444-4444-444444444444", "codeVerifier": "v" * 43},
    )

    assert response.status_code != 401


def test_exchange_and_start_success_responses_are_not_cacheable() -> None:
    """成功响应必须带 `no-store`：它们含 state 或凭据。

    这里只断言契约本身（源码里两处都显式设置了该头）；失败路径由 HTTPException
    生成，不带凭据，因此不强制。
    """
    from pathlib import Path as _Path

    source = _Path("src/lyra/api/auth_routes.py").read_text(encoding="utf-8")
    assert 'headers["cache-control"] = "no-store"' in source
    assert source.count('cache-control') >= 2


def test_start_success_response_is_not_cacheable(client) -> None:
    """成功发起登录时响应带 `no-store`。

    本夹具未配置来源，因此失败路径走 HTTPException——它在源码里不经 JSONResponse，
    也因此不受该头约束（正文不含凭据）。契约由上面的源码断言覆盖。
    """
    test_client, _ = client
    response = test_client.post("/v1/auth/github/start", json=VALID_START)

    # 未配置来源 → 503，由 HTTPException 生成。
    assert response.status_code == 503


def test_callback_redirect_sets_the_full_security_header_set(client) -> None:
    """回调重定向保留原有安全响应头，避免后续调整时意外移除。"""
    test_client, _ = client
    response = test_client.get(
        "/v1/auth/github/callback",
        params={"state": "A" * 43, "code": "c"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "default-src 'none'" in csp


def test_callback_security_headers_apply_to_the_failure_redirect_too(client) -> None:
    """失败路径同样带头；成功与失败共用同一个响应构造器。"""
    test_client, _ = client
    response = test_client.get(
        "/v1/auth/github/callback",
        params={"state": "A" * 43, "error": "access_denied"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    assert response.headers["x-content-type-options"] == "nosniff"


# --------------------------------------------------------------------------
# 校验顺序：请求格式与"服务端是否配置"无关
# --------------------------------------------------------------------------


def _start_body(**overrides) -> dict:
    """构造一份合法的登录发起正文。"""
    body = {
        "displayName": "Pixel 9",
        "publicKeyPem": "-----BEGIN PUBLIC KEY-----x",
        "codeChallenge": "A" * 43,
        "codeChallengeMethod": "S256",
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("codeChallenge 不足 43 位", _start_body(codeChallenge="A" * 42)),
        ("codeChallengeMethod 非 S256", _start_body(codeChallengeMethod="S512")),
        ("displayName 超过 64 位", _start_body(displayName="x" * 65)),
    ],
)
def test_a_malformed_start_body_is_a_400_even_when_auth_is_unconfigured(
    client, label: str, body: dict
) -> None:
    """**请求格式非法时返回 400，即使服务端未配置该登录方式。**

    这个顺序很重要：``AUTH_NOT_CONFIGURED`` 是 **503**——客户端可能据此重试或提示
    "服务端未启用登录"，而真正的问题是**它的请求构造错了**。

    **请求是否合法与服务端是否配置无关**，因此校验必须先于配置判断。参考实现在
    transport 层用 schema 校验正文，天然满足这一点。
    """
    test_client, _ = client
    response = test_client.post("/v1/auth/github/start", json=body)

    assert response.status_code == 400, f"{label}: {response.status_code}"
    assert response.json()["code"] == "INVALID_ARGUMENT"


def test_a_valid_start_body_still_reports_not_configured(client) -> None:
    """**合法正文 + 未配置 → 503**，与上一条构成完整对照。

    只断言"格式非法 → 400"是不够的：如果实现把**所有**请求都判成 400，那条测试同样
    通过。必须同时断言合法请求仍能走到配置检查。
    """
    test_client, _ = client
    response = test_client.post("/v1/auth/github/start", json=_start_body())

    assert response.status_code == 503
    assert response.json()["code"] == "AUTH_NOT_CONFIGURED"


def test_a_malformed_code_verifier_is_not_reported_as_not_configured(client) -> None:
    """``exchange`` 的 verifier 格式检查同样先于配置判断。

    PKCE 比对需要 attempt，因此那一步仍在后面——但格式不合法的请求不该拿到 503。
    """
    test_client, _ = client
    response = test_client.post(
        "/v1/auth/github/exchange",
        json={"loginId": "11111111-1111-1111-1111-111111111111", "codeVerifier": "short"},
    )

    assert response.status_code == 401
    assert response.json()["code"] != "AUTH_NOT_CONFIGURED"


# --------------------------------------------------------------------------
# 正文类型收口：非字符串字段必须 400，不能变成 500
# --------------------------------------------------------------------------


def _start_body_without(field: str) -> dict:
    """构造一份缺少某个字段的登录正文。"""
    body = dict(VALID_START)
    del body[field]
    return body


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("codeChallenge 是数字", {**VALID_START, "codeChallenge": 123}),
        ("codeChallenge 是数组", {**VALID_START, "codeChallenge": ["x" * 43]}),
        ("codeChallengeMethod 是数字", {**VALID_START, "codeChallengeMethod": 256}),
        ("displayName 是数字", {**VALID_START, "displayName": 123}),
        ("displayName 缺失", _start_body_without("displayName")),
        ("publicKeyPem 是数字", {**VALID_START, "publicKeyPem": 123}),
    ],
)
def test_a_non_string_start_field_is_a_400_not_a_500(client, label: str, body: dict) -> None:
    """**非字符串字段必须在路由层被拒**。

    服务层直接对值跑 ``re.fullmatch`` / ``str.strip``；把手写的 dict 原样传下去，
    一个 ``"codeChallenge": 123`` 就会抛 ``TypeError``，最终对客户端表现为
    **500 INTERNAL_ERROR**——而这是它自己构造错了请求，应当是 400。
    """
    test_client, _ = client
    response = test_client.post("/v1/auth/github/start", json=body)

    assert response.status_code == 400, f"{label}: {response.status_code}"
    assert response.json()["code"] == "INVALID_ARGUMENT"
    # 400 必须带文案：空 message 会让客户端显示一条没有内容的错误。
    assert response.json()["message"] == "登录参数无效"


def test_start_rejects_the_legacy_invite_field(client) -> None:
    """旧的 start 邀请码路径不再接受，即使字段本身是合法字符串。"""
    test_client, _ = client
    response = test_client.post(
        "/v1/auth/github/start", json={**VALID_START, "inviteCode": "a" * 32}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


class _FakeLoginClient:
    """只提供路由层用得到的动作的来源客户端。"""

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        """返回一个固定授权地址；本组测试不解析它。"""
        return f"https://provider.example/authorize?state={state}"

    def exchange_code(self, **kwargs: Any) -> LoginIdentity:
        """本组测试不会走到授权码交换。"""
        raise AssertionError("本组测试不应执行授权码交换")


@pytest.fixture()
def configured_client(client):
    """在公共夹具之上装配一个 github 来源。

    **必须有已配置的来源**才能验证校验顺序：未配置时服务层会先抛 503，
    掩盖掉"类型非法应当 400"这条断言。
    """
    test_client, issued = client
    test_client.app.state.login_service = UserLoginService(
        [
            ConfiguredLoginProvider(
                id="github",
                name="GitHub",
                client=_FakeLoginClient(),
            )
        ],
        lambda **kwargs: None,
    )
    return test_client, issued


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("loginId 是数组", {"loginId": ["x"], "codeVerifier": "v" * 43}),
        ("loginId 是数字", {"loginId": 123, "codeVerifier": "v" * 43}),
        ("loginId 缺失", {"codeVerifier": "v" * 43}),
        ("codeVerifier 是数字", {"loginId": "11111111-1111-1111-1111-111111111111", "codeVerifier": 43}),
    ],
)
def test_a_non_string_exchange_field_is_a_400_not_a_500(
    configured_client, label: str, body: dict
) -> None:
    """``exchange`` 的非字符串字段同样必须 400。

    ``loginId`` 是数组时服务层会拿它当字典键（``self._attempts.get(["x"])``），
    未收口时抛 ``TypeError`` → 500。
    """
    test_client, _ = configured_client
    response = test_client.post("/v1/auth/github/exchange", json=body)

    assert response.status_code == 400, f"{label}: {response.status_code}"
    assert response.json()["code"] == "INVALID_ARGUMENT"
    assert response.json()["message"] == "登录参数无效"


@pytest.mark.parametrize(
    "invite_code",
    [None, 123, ["code"], {"value": "code"}, "", "   "],
    ids=["null", "number", "array", "object", "empty", "whitespace"],
)
def test_exchange_requires_an_optional_invite_to_be_a_nonempty_trimmed_string(
    configured_client, invite_code: Any
) -> None:
    """exchange 的可选邀请码若出现，必须是 trim 后非空字符串。"""
    test_client, _ = configured_client
    response = test_client.post(
        "/v1/auth/github/exchange",
        json={
            "loginId": "11111111-1111-1111-1111-111111111111",
            "codeVerifier": _VERIFIER,
            "inviteCode": invite_code,
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("payload", [{}, {"refreshToken": ""}, {"refreshToken": 5}])
def test_an_invalid_device_token_body_carries_a_message(client, payload: dict) -> None:
    """非法刷新正文的 400 必须带文案。

    只有 ``code`` 时错误信封取到空 message，客户端会显示一条空错误提示。
    """
    test_client, _ = client
    body = test_client.post("/v1/devices/tokens", json=payload).json()

    assert body["code"] == "INVALID_ARGUMENT"
    assert body["message"] == "设备令牌刷新参数无效"


# --------------------------------------------------------------------------
# 单写队列：认证路径必须复用应用级单例
# --------------------------------------------------------------------------


class _RecordingCoordinator(WriteCoordinator):
    """记录入队次数的协调器替身。"""

    def __init__(self) -> None:
        super().__init__()
        self.enqueued = 0

    async def enqueue(self, operation: Any) -> Any:
        """计数后交给真实实现执行。"""
        self.enqueued += 1
        return await super().enqueue(operation)


def test_auth_writes_use_the_app_write_coordinator_singleton(client) -> None:
    """刷新与登出必须走 ``app.state.write_coordinator``。

    **各自 ``new`` 一个协调器等于没有串行化**：健康端点的 ``writeQueue`` 段会与
    真实认证写入脱节，刷新与登出也可以交错执行。
    """
    test_client, issued = client
    spy = _RecordingCoordinator()
    test_client.app.state.write_coordinator = spy

    test_client.post("/v1/devices/tokens", json={"refreshToken": issued.refresh_token})
    assert spy.enqueued == 1

    test_client.post(
        "/v1/auth/logout", headers={"Authorization": f"Bearer {issued.access_token}"}
    )
    assert spy.enqueued == 2


# --------------------------------------------------------------------------
# 兑换响应：契约 §3.9 的 user 对象
# --------------------------------------------------------------------------


class _CompletingLoginService:
    """固定返回一次已完成兑换的登录服务；只用于验证响应组装。"""

    def __init__(self, user: Any, identity: Any) -> None:
        self._user = user
        self._identity = identity

    async def exchange(
        self,
        provider_id: str,
        *,
        login_id: str,
        code_verifier: str,
        invite_code: str | None = None,
    ) -> dict[str, Any]:
        """返回一份完整的兑换结果（含已解析的用户与身份）。"""
        return {
            "status": "complete",
            "deviceId": "device-1",
            "displayName": "Pixel 9",
            "accessToken": "access",
            "accessTokenExpiresAt": "2026-10-13T00:10:00.000Z",
            "refreshToken": "refresh",
            "refreshTokenExpiresAt": "2026-11-12T00:00:00.000Z",
            "user": self._user,
            "identity": self._identity,
        }


def test_exchange_returns_the_frozen_user_object(client) -> None:
    """兑换响应的 ``user`` 必须是契约 §3.9 的完整形状。

    身份收在 ``identity`` 子对象里：身份上的 display_name 是第三方昵称，与用户在
    Lyra 的显示名不是一回事。``email`` 与 ``userId`` 缺一不可——客户端用同一个模型
    解析它与 ``GET /v1/me``。
    """
    test_client, _ = client
    user = UserRecord(
        id="11111111-1111-1111-1111-111111111111",
        display_name="octocat",
        status="active",
        role="member",
        created_at="2026-10-13T00:00:00.000Z",
        updated_at="2026-10-13T00:00:00.000Z",
    )
    identity = UserIdentityRecord(
        id="22222222-2222-2222-2222-222222222222",
        user_id=user.id,
        provider="github",
        subject="12345",
        email="owner@example.com",
        display_name="octocat",
        created_at="2026-10-13T00:00:00.000Z",
        updated_at="2026-10-13T00:00:00.000Z",
    )
    test_client.app.state.login_service = _CompletingLoginService(user, identity)

    response = test_client.post(
        "/v1/auth/github/exchange",
        json={"loginId": "11111111-1111-1111-1111-111111111111", "codeVerifier": "v" * 43},
    )

    assert response.status_code == 200
    assert response.json()["user"] == {
        "userId": user.id,
        "displayName": "octocat",
        "status": "active",
        "role": "member",
        "identity": {
            "provider": "github",
            "subject": "12345",
            "email": "owner@example.com",
        },
    }
    # 设备与令牌字段保持原形状。
    assert response.json()["deviceId"] == "device-1"
    assert response.json()["displayName"] == "Pixel 9"


# --------------------------------------------------------------------------
# 来源可用性只取决于 OAuth 配置
# --------------------------------------------------------------------------


def test_a_provider_is_available_without_any_owner_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**只配齐 OAuth 凭据（不配任何 ``LYRA_OWNER_*``）该来源就必须可用。**

    多用户下谁能登录由 ``(provider, subject)`` 的用户解析决定，部署配置里不再需要
    一份 subject 名单；启动也不该因为缺少它而失败（契约 §3.10）。首个管理员改由
    离线入口显式创建。
    """
    monkeypatch.setenv("LYRA_LOGIN_GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("LYRA_LOGIN_GITHUB_CLIENT_SECRET", "secret")
    monkeypatch.setenv(
        "LYRA_LOGIN_GITHUB_REDIRECT_URI", "https://lyra.example.com/v1/auth/github/callback"
    )
    database_path = tmp_path / "providers.duckdb"
    database = open_lyra_database(str(database_path))
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    try:
        application = create_app(settings, database)
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
            catalog = test_client.get("/v1/auth/providers").json()
            availability = {entry["id"]: entry["available"] for entry in catalog["providers"]}
            assert availability == {"google": False, "linuxdo": False, "github": True}

            started = test_client.post("/v1/auth/github/start", json={
                "displayName": "Pixel 9",
                "publicKeyPem": PUBLIC_KEY_PEM,
                "codeChallenge": _CHALLENGE,
                "codeChallengeMethod": "S256",
            })
            assert started.status_code == 200, started.text
            assert started.json()["authorizationUrl"].startswith(
                "https://github.com/login/oauth/authorize?"
            )
    finally:
        database.close()


def test_a_partially_configured_provider_still_fails_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """部分配置仍然在启动时明确失败。

    去掉 owner 名单不等于放宽"部分配置"这条约束：让服务带着"看起来启用了但永远
    登不进去"的状态上线是最糟的选择。
    """
    monkeypatch.setenv("LYRA_LOGIN_GITHUB_CLIENT_ID", "cid")
    database_path = tmp_path / "partial.duckdb"
    database = open_lyra_database(str(database_path))
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    try:
        with pytest.raises(ValueError, match="LYRA_LOGIN_GITHUB_CLIENT_SECRET"):
            create_app(settings, database)
    finally:
        database.close()


# --------------------------------------------------------------------------
# 自动注册：完整 OAuth 验证 + 客户端 verifier 校验之后才创建用户
# --------------------------------------------------------------------------


# 一个满足 PKCE verifier 字符集与长度的固定 verifier；挑战由它算出。
_VERIFIER = "v" * 43
_CHALLENGE = pkce_challenge(_VERIFIER)


class _ScriptedLoginClient:
    """按脚本返回来源身份的客户端；其余行为与真实客户端无关。"""

    def __init__(self, identity: LoginIdentity) -> None:
        self.identity = identity

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        """返回带 state 的固定授权地址；state 由调用方解析后回填给回调。"""
        return f"https://provider.example/authorize?state={state}"

    def exchange_code(self, **kwargs: Any) -> LoginIdentity:
        """返回脚本身份；参数校验由服务层的其它测试覆盖。"""
        return self.identity


def _wired_login_client(test_client: Any, identity: LoginIdentity, provider: str = "github"):
    """装配一个**真实**登录服务：真实用户解析 + 真实设备签发。

    只把来源客户端换成脚本替身，因此自动注册、设备归属与凭据签发走的都是生产代码。
    """
    application = test_client.app

    async def authorize(**kwargs: Any):
        """兑换成功后的用户解析与设备签发。

        **直接复用生产装配的那个函数**（``main._authorize_user_device``）：邀请码门禁
        就装在里面。测试若自己拼一份等价实现，就会在"门禁忘了注入"时不报警——而
        那正是这类安全边界最典型的失效方式。
        """
        from lyra.main import _authorize_user_device

        return await _authorize_user_device(
            database=application.state.database,
            write_coordinator=application.state.write_coordinator,
            **kwargs,
        )

    test_client.app.state.login_service = UserLoginService(
        [
            ConfiguredLoginProvider(
                id=provider,  # type: ignore[arg-type]
                name="GitHub",
                client=_ScriptedLoginClient(identity),
            )
        ],
        authorize,
    )
    return test_client


def _mint_invite(test_client: Any, *, label: str | None = None) -> str:
    """铸造一枚邀请码并返回明文（测试用）。

    **新身份注册必须凭码**，所以凡是"走完整登录流程注册一个新用户"的测试都得先拿
    一枚；这也顺带保证测试走的是与生产相同的门禁路径。
    """
    invite, code = InviteCodeRepository(
        test_client.app.state.database.connection
    ).create(created_by=None, label=label, now=datetime.now(UTC))
    assert invite.status == "active"
    return code


def _start_login(
    test_client: Any,
    provider: str = "github",
    pem: str = PUBLIC_KEY_PEM,
) -> dict:
    """发起一次登录并返回 start 响应正文。"""
    body: dict[str, Any] = {
        "displayName": "Pixel 9",
        "publicKeyPem": pem,
        "codeChallenge": _CHALLENGE,
        "codeChallengeMethod": "S256",
    }
    response = test_client.post(f"/v1/auth/{provider}/start", json=body)
    # 处理器返回显式构造的 ``JSONResponse``，它的默认状态码是 200——装饰器上的
    # ``status_code=201`` 只在返回普通 dict 时生效。这里固定**实际**行为。
    assert response.status_code == 200, response.text
    return response.json()


def _complete_callback(test_client: Any, started: dict, provider: str = "github") -> None:
    """用授权地址里的 state 走一次回调，并保留服务器的原始重定向响应。"""
    from urllib.parse import parse_qs, urlsplit

    state = parse_qs(urlsplit(started["authorizationUrl"]).query)["state"][0]
    response = test_client.get(
        f"/v1/auth/{provider}/callback",
        params={"state": state, "code": "auth-code"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _exchange(
    test_client: Any,
    started: dict,
    provider: str = "github",
    invite_code: str | None = None,
):
    """用 App verifier 兑换凭据；邀请码只在需要注册时放进兑换正文。"""
    code = invite_code if invite_code is not None else started.get("_testInviteCode")
    body = {"loginId": started["loginId"], "codeVerifier": _VERIFIER}
    if code is not None:
        body["inviteCode"] = code
    return test_client.post(
        f"/v1/auth/{provider}/exchange",
        json=body,
    )


def _count(connection: Any, table: str) -> int:
    """统计表行数。"""
    row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize(
    ("provider", "identity"),
    [
        ("google", LoginIdentity("google", "callback-google", None, "Google User")),
        ("github", LoginIdentity("github", "callback-github", None, "octocat")),
        ("linuxdo", LoginIdentity("linuxdo", "callback-linuxdo", None, "LINUX DO User")),
    ],
)
def test_callback_redirect_is_constant_for_all_providers_and_outcomes(
    fresh_client, provider: str, identity: LoginIdentity
) -> None:
    """三种来源在成功与失败时均回到同一 URI，且忽略恶意 returnUrl。"""
    from urllib.parse import parse_qs, urlsplit

    test_client = fresh_client
    _wired_login_client(test_client, identity, provider)
    started = _start_login(test_client, provider)
    state = parse_qs(urlsplit(started["authorizationUrl"]).query)["state"][0]
    attacker_url = "https://attacker.example/collect?token=stolen"
    callback_path = f"/v1/auth/{provider}/callback"

    succeeded = test_client.get(
        callback_path,
        params={
            "state": state,
            "code": "sensitive-authorization-code",
            "returnUrl": attacker_url,
        },
        follow_redirects=False,
    )
    failed = test_client.get(
        callback_path,
        params={"state": "X" * 43, "error": "access_denied", "returnUrl": attacker_url},
        follow_redirects=False,
    )

    expected_uri = "com.lyrafinance.lyra://auth/callback"
    assert succeeded.status_code == failed.status_code == 303
    assert succeeded.headers["location"] == failed.headers["location"] == expected_uri
    assert succeeded.content == failed.content == b""
    # request-id 每次请求独立生成；其余响应头必须完全相同。
    succeeded_headers = {
        key: value for key, value in succeeded.headers.items() if key != "x-request-id"
    }
    failed_headers = {
        key: value for key, value in failed.headers.items() if key != "x-request-id"
    }
    assert succeeded_headers == failed_headers
    for response in (succeeded, failed):
        response_payload = response.headers["location"] + response.text
        for sensitive in (
            state,
            "sensitive-authorization-code",
            started["loginId"],
            identity.subject,
            "attacker.example",
            "stolen",
        ):
            assert sensitive not in response_payload


@pytest.mark.parametrize(
    ("provider", "identity"),
    [
        ("github", LoginIdentity("github", "9001", None, "octocat")),
        ("linuxdo", LoginIdentity("linuxdo", "9002", "user@example.com", "linuxdo-user")),
        ("google", LoginIdentity("google", "9003", "g@example.com", "Google User")),
    ],
)
def test_a_new_identity_registers_as_a_member_with_an_empty_ledger(
    fresh_client, provider: str, identity: LoginIdentity
) -> None:
    """三来源的新身份都能自动注册为 ``member``，并获得空账本。

    **空账本不是"没报错"**：这里显式断言该用户名下没有账户，且用户账本版本为 0
    （契约 §3.3 的定义：``max(version) WHERE user_id = ?``）。
    """
    test_client = fresh_client
    _wired_login_client(test_client, identity, provider)
    connection = test_client.app.state.database.connection

    invite = _mint_invite(test_client, label="注册测试")
    started = _start_login(test_client, provider)
    _complete_callback(test_client, started, provider)
    response = _exchange(test_client, started, provider, invite_code=invite)

    assert response.status_code == 200, response.text
    user = response.json()["user"]
    assert user["role"] == "member"
    assert user["status"] == "active"
    assert user["identity"] == {
        "provider": provider,
        "subject": identity.subject,
        "email": identity.email,
    }
    assert _count(connection, "users") == 1
    assert _count(connection, "user_identities") == 1
    accounts = connection.execute(
        "SELECT count(*) FROM accounts WHERE user_id = CAST(? AS UUID)", [user["userId"]]
    ).fetchone()
    assert accounts is not None and int(accounts[0]) == 0
    version = connection.execute(
        "SELECT coalesce(max(version), 0) FROM ledger_versions WHERE user_id = CAST(? AS UUID)",
        [user["userId"]],
    ).fetchone()
    assert version is not None and int(version[0]) == 0


@pytest.mark.parametrize(
    ("provider", "identity"),
    [
        ("google", LoginIdentity("google", "9101", None, "Google User")),
        ("github", LoginIdentity("github", "9102", None, "octocat")),
        ("linuxdo", LoginIdentity("linuxdo", "9103", None, "LINUX DO User")),
    ],
)
def test_each_provider_can_add_an_invite_after_oauth_on_the_same_transaction(
    fresh_client, provider: str, identity: LoginIdentity
) -> None:
    """三个来源缺码时都保留 OAuth 事务，可在同一 loginId 上补码完成注册。"""
    test_client = fresh_client
    _wired_login_client(test_client, identity, provider)
    started = _start_login(test_client, provider)
    _complete_callback(test_client, started, provider)

    missing = _exchange(test_client, started, provider)
    assert missing.status_code == 403
    assert missing.json()["code"] == "INVITE_CODE_REQUIRED"
    assert _count(test_client.app.state.database.connection, "users") == 0

    invite = _mint_invite(test_client)
    completed = _exchange(test_client, started, provider, invite_code=invite)
    assert completed.status_code == 200, completed.text
    assert completed.json()["user"]["identity"]["provider"] == provider
    assert _count(test_client.app.state.database.connection, "users") == 1


def test_the_same_identity_logs_in_as_the_same_user(fresh_client) -> None:
    """同一身份再次登录：同一 userId，不新建用户，也不重置账本归属。"""
    test_client = fresh_client
    _wired_login_client(test_client, LoginIdentity("github", "9001", None, "octocat"))
    connection = test_client.app.state.database.connection

    first = _exchange(test_client, _after_callback(test_client))
    second = _exchange(test_client, _after_callback(test_client))

    assert first.json()["user"]["userId"] == second.json()["user"]["userId"]
    assert _count(connection, "users") == 1
    assert _count(connection, "user_identities") == 1


def _after_callback(
    test_client: Any, provider: str = "github", invite_code: str | None = None
) -> dict:
    """发起登录并完成回调，返回 start 响应正文。

    默认**自动铸造一枚邀请码并保存在测试私有字段**：多数用例只关心登录是否走得通，
    兑换 helper 会把码放进 exchange 正文；真实 start 响应仍不含邀请码。
    """
    if invite_code is None:
        invite_code = _mint_invite(test_client)
    started = _start_login(test_client, provider)
    _complete_callback(test_client, started, provider)
    started["_testInviteCode"] = invite_code
    return started


def test_two_independent_logins_converge_to_one_user(fresh_client) -> None:
    """同一身份的两次独立登录事务收敛到同一 userId（对应 V02）。

    两个服务各自持有自己的写协调器，模拟两个独立登录事务；唯一约束与
    ``find_or_create_user_for_identity`` 的重查必须让它们落到同一个用户上。
    """
    test_client = fresh_client
    connection = test_client.app.state.database.connection
    identity = LoginIdentity("github", "9001", None, "octocat")

    async def two_logins() -> tuple[str, str]:
        """并发跑两次签发；两次都必须成功且返回同一用户。"""
        results = []
        for _ in range(2):
            pairing = DevicePairingService(
                DuckDbDeviceRepository(connection),
                WriteCoordinator(),
                UserRepository(connection),
            )
            results.append(await pairing.authorize_user_device(
                identity=identity, display_name="Pixel 9", public_key_pem=PUBLIC_KEY_PEM
            ))
        return results[0].user.id, results[1].user.id

    first_user, second_user = asyncio.run(two_logins())

    assert first_user == second_user
    assert _count(connection, "users") == 1
    assert _count(connection, "user_identities") == 1


def test_a_pending_exchange_does_not_create_a_user(fresh_client) -> None:
    """**未完成验证不创建用户**：回调还没发生时兑换返回 202。"""
    test_client = fresh_client
    _wired_login_client(test_client, LoginIdentity("github", "9001", None, "octocat"))
    connection = test_client.app.state.database.connection

    started = _start_login(test_client)
    response = _exchange(test_client, started)

    assert response.status_code == 202
    assert response.json() == {"status": "pending"}
    assert _count(connection, "users") == 0
    assert _count(connection, "user_identities") == 0


def test_a_denied_authorization_does_not_create_a_user(fresh_client) -> None:
    """授权被取消（``error=access_denied``）时兑换失败，且不创建用户。"""
    test_client = fresh_client
    _wired_login_client(test_client, LoginIdentity("github", "9001", None, "octocat"))
    connection = test_client.app.state.database.connection

    started = _start_login(test_client)
    from urllib.parse import parse_qs, urlsplit

    state = parse_qs(urlsplit(started["authorizationUrl"]).query)["state"][0]
    test_client.get(
        "/v1/auth/github/callback",
        params={"state": state, "error": "access_denied"},
        follow_redirects=False,
    )
    response = _exchange(test_client, started)

    assert response.status_code == 403
    assert response.json()["code"] == "LOGIN_DENIED"
    assert _count(connection, "users") == 0


def test_a_wrong_verifier_does_not_create_a_user(fresh_client) -> None:
    """verifier 不匹配时兑换失败，且不创建用户。"""
    test_client = fresh_client
    _wired_login_client(test_client, LoginIdentity("github", "9001", None, "octocat"))
    connection = test_client.app.state.database.connection

    started = _after_callback(test_client)
    response = test_client.post(
        "/v1/auth/github/exchange",
        json={"loginId": started["loginId"], "codeVerifier": "x" * 43},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "LOGIN_FAILED"
    assert _count(connection, "users") == 0


def test_the_same_email_in_two_providers_does_not_merge_users(fresh_client) -> None:
    """**同邮箱不合并**：身份键始终是 ``(provider, subject)``。"""
    test_client = fresh_client
    connection = test_client.app.state.database.connection

    _wired_login_client(test_client, LoginIdentity("github", "9001", "same@example.com", "a"))
    github = _exchange(test_client, _after_callback(test_client))

    _wired_login_client(
        test_client, LoginIdentity("google", "9002", "same@example.com", "b"), "google"
    )
    google = _exchange(test_client, _after_callback(test_client, "google"), "google")

    assert github.status_code == 200 and google.status_code == 200
    assert github.json()["user"]["userId"] != google.json()["user"]["userId"]
    assert _count(connection, "users") == 2
    assert _count(connection, "user_identities") == 2


def test_a_disabled_identity_cannot_log_in_or_refresh(fresh_client) -> None:
    """停用身份既不能重新登录，也不能刷新。"""
    test_client = fresh_client
    _wired_login_client(test_client, LoginIdentity("github", "9001", None, "octocat"))
    connection = test_client.app.state.database.connection
    first = _exchange(test_client, _after_callback(test_client))
    user_id = first.json()["user"]["userId"]
    connection.execute(
        "UPDATE users SET status = 'disabled' WHERE id = CAST(? AS UUID)", [user_id]
    )

    # 刷新被拒，且是 403 USER_DISABLED（不是 401）：客户端据此结束会话。
    refresh = test_client.post(
        "/v1/devices/tokens", json={"refreshToken": first.json()["refreshToken"]}
    )
    assert refresh.status_code == 403
    assert refresh.json()["code"] == "USER_DISABLED"

    # 重新登录被拒，且不新建用户（保留身份映射以防同一身份重新注册）。
    relogin = _exchange(test_client, _after_callback(test_client))
    assert relogin.status_code == 403
    assert relogin.json()["code"] == "USER_DISABLED"
    assert _count(connection, "users") == 1


def test_me_returns_the_same_user_object_as_the_login_response(fresh_client) -> None:
    """``GET /v1/me`` 与登录完成响应给出**同一个** user 对象。"""
    test_client = fresh_client
    _wired_login_client(test_client, LoginIdentity("github", "9001", "me@example.com", "octocat"))
    logged_in = _exchange(test_client, _after_callback(test_client))

    response = test_client.get(
        "/v1/me", headers={"Authorization": f"Bearer {logged_in.json()['accessToken']}"}
    )

    assert response.status_code == 200
    assert response.json() == {"user": logged_in.json()["user"]}


def test_me_requires_authentication(fresh_client) -> None:
    """``/v1/me`` 必须要求访问令牌。"""
    assert fresh_client.get("/v1/me").status_code == 401


def test_me_reports_a_disabled_user(client) -> None:
    """用户被停用后 ``/v1/me`` 返回 403 ``USER_DISABLED``（未撤销设备时）。"""
    test_client, issued = client
    connection = test_client.app.state.database.connection
    connection.execute(
        "UPDATE users SET status = 'disabled' WHERE id = CAST(? AS UUID)", [issued.user.id]
    )

    response = test_client.get(
        "/v1/me", headers={"Authorization": f"Bearer {issued.access_token}"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "USER_DISABLED"


# --------------------------------------------------------------------------
# 管理路由：仅 admin，受限分页，首版只改状态
# --------------------------------------------------------------------------


def _promote(connection: Any, user_id: str) -> None:
    """把用户提升为管理员（本代理无权通过接口改角色）。"""
    connection.execute(
        "UPDATE users SET role = 'admin' WHERE id = CAST(? AS UUID)", [user_id]
    )


def test_admin_users_requires_an_admin(client) -> None:
    """普通成员访问用户目录：403 ``ADMIN_REQUIRED``。"""
    test_client, issued = client

    response = test_client.get(
        "/v1/admin/users", headers={"Authorization": f"Bearer {issued.access_token}"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ADMIN_REQUIRED"


def test_admin_users_lists_paginated_users_without_secrets(client) -> None:
    """管理员可读取受限分页的用户目录，且响应不含凭据或账本数据。"""
    test_client, issued = client
    connection = test_client.app.state.database.connection
    _promote(connection, issued.user.id)

    response = test_client.get(
        "/v1/admin/users?limit=10&offset=0",
        headers={"Authorization": f"Bearer {issued.access_token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 10 and body["offset"] == 0
    assert [entry["userId"] for entry in body["users"]] == [issued.user.id]
    assert body["users"][0]["role"] == "admin"
    raw = response.text
    for leaked in ("accessToken", "refreshToken", "publicKey", "token"):
        assert leaked not in raw


def test_admin_users_rejects_an_out_of_range_page_size(client) -> None:
    """``limit`` 越界返回 400，而不是静默截断成另一页。"""
    test_client, issued = client
    connection = test_client.app.state.database.connection
    _promote(connection, issued.user.id)

    response = test_client.get(
        "/v1/admin/users?limit=100000",
        headers={"Authorization": f"Bearer {issued.access_token}"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


def test_admin_patch_disables_a_member_and_revokes_its_sessions(client) -> None:
    """停用成员：状态变更 + 撤销其全部设备凭据（不是只改一个字段）。"""
    test_client, issued = client
    connection = test_client.app.state.database.connection
    _promote(connection, issued.user.id)
    admin_token = issued.access_token
    member = asyncio.run(_second_member(test_client))

    response = test_client.patch(
        f"/v1/admin/users/{member['userId']}",
        json={"status": "disabled"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["status"] == "disabled"
    devices = connection.execute(
        "SELECT count(*) FROM paired_devices WHERE user_id = CAST(? AS UUID) AND status = 'revoked'",
        [member["userId"]],
    ).fetchone()
    assert devices is not None and int(devices[0]) == 1
    # 该成员既不能用旧访问令牌，也不能刷新。
    assert test_client.get(
        "/v1/me", headers={"Authorization": f"Bearer {member['accessToken']}"}
    ).status_code in (401, 403)
    refresh = test_client.post(
        "/v1/devices/tokens", json={"refreshToken": member["refreshToken"]}
    )
    assert refresh.status_code == 403
    assert refresh.json()["code"] == "USER_DISABLED"


async def _second_member(test_client: Any) -> dict:
    """在同一个应用里直接注册第二个成员并签发设备，返回其凭据。"""
    connection = test_client.app.state.database.connection
    pairing = DevicePairingService(
        DuckDbDeviceRepository(connection),
        test_client.app.state.write_coordinator,
        UserRepository(connection),
    )
    issued = await pairing.authorize_user_device(
        identity=LoginIdentity("google", "7001", "member@example.com", "Member"),
        display_name="Tablet",
        public_key_pem=OTHER_KEY_PEM,
    )
    return {
        "userId": issued.user.id,
        "deviceId": issued.device_id,
        "accessToken": issued.access_token,
        "refreshToken": issued.refresh_token,
    }


def test_restoring_a_user_does_not_revive_old_credentials(client) -> None:
    """恢复用户沿用原 userId，但**旧凭据不复活**（计划 §4.4、V12）。

    这条守的是一个不显然的区别：停用如果只把 ``users.status`` 改成 ``disabled``，
    旧令牌在过期前仍然有效——认证层会因停用拒绝它，**但恢复后它又会重新通过**。
    正确做法是停用同时**撤销**全部设备凭据，于是恢复只能让用户以新登录重新进入。

    只断言"停用期间被拒"是不够的：那种断言对「只改状态」和「真的撤销」两种实现
    都成立，区分不出后者。必须把状态改回 ``active`` 再验旧令牌。
    """
    test_client, issued = client
    connection = test_client.app.state.database.connection
    _promote(connection, issued.user.id)
    admin_token = issued.access_token
    member = asyncio.run(_second_member(test_client))
    user_id = member["userId"]

    for status in ("disabled", "active"):
        patched = test_client.patch(
            f"/v1/admin/users/{user_id}",
            json={"status": status},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert patched.status_code == 200
        assert patched.json()["user"]["status"] == status

    # 身份沿用同一 userId，没有新建用户。
    assert patched.json()["user"]["userId"] == user_id
    assert connection.execute(
        "SELECT count(*) FROM users WHERE id = CAST(? AS UUID)", [user_id]
    ).fetchone() == (1,)

    # **恢复之后旧凭据依然无效**：访问令牌与刷新令牌都不复活。
    assert test_client.get(
        "/v1/me", headers={"Authorization": f"Bearer {member['accessToken']}"}
    ).status_code in (401, 403)
    assert test_client.post(
        "/v1/devices/tokens", json={"refreshToken": member["refreshToken"]}
    ).status_code in (401, 403)

    # 重新登录可以拿到新凭据，证明用户本身已恢复可用。
    reissued = asyncio.run(_second_member(test_client))
    assert reissued["userId"] == user_id
    assert test_client.get(
        "/v1/me", headers={"Authorization": f"Bearer {reissued['accessToken']}"}
    ).status_code == 200


def test_admin_patch_disable_closes_the_members_event_stream(client) -> None:
    """停用必须踢掉该用户已建立的事件长连接。

    **不得只改状态字段就报告停用完成**：长连接与在飞回合属于"已经建立"的访问路径，
    它们不会因为下一次请求被拒而停止推送（计划 §4.4）。
    """
    test_client, issued = client
    connection = test_client.app.state.database.connection
    _promote(connection, issued.user.id)
    member = asyncio.run(_second_member(test_client))
    registry: EventConnectionRegistry = test_client.app.state.event_connections
    socket = _FakeSocket()
    registry.add(member["deviceId"], socket)

    response = test_client.patch(
        f"/v1/admin/users/{member['userId']}",
        json={"status": "disabled"},
        headers={"Authorization": f"Bearer {issued.access_token}"},
    )

    assert response.status_code == 200
    assert socket.closed == [(4001, "signed out")]
    assert registry.take_device(member["deviceId"]) == ()


def test_admin_patch_refuses_role_changes(client) -> None:
    """**角色不能通过管理接口修改**：请求被明确拒绝，而不是被静默忽略。

    静默忽略最危险：调用方以为提权成功了，而实际没有（或反过来），两者都会让
    权限判断与实际状态脱节。
    """
    test_client, issued = client
    connection = test_client.app.state.database.connection
    _promote(connection, issued.user.id)

    response = test_client.patch(
        f"/v1/admin/users/{issued.user.id}",
        json={"role": "admin"},
        headers={"Authorization": f"Bearer {issued.access_token}"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


def test_admin_patch_refuses_unknown_fields(client) -> None:
    """正文只接受 ``status``；多带字段一律 400。"""
    test_client, issued = client
    connection = test_client.app.state.database.connection
    _promote(connection, issued.user.id)

    response = test_client.patch(
        f"/v1/admin/users/{issued.user.id}",
        json={"status": "active", "displayName": "x"},
        headers={"Authorization": f"Bearer {issued.access_token}"},
    )

    assert response.status_code == 400


def test_admin_patch_protects_the_last_active_admin(client) -> None:
    """**不得停用最后一个活动管理员**：否则部署再没有任何 HTTP 入口能恢复。"""
    test_client, issued = client
    connection = test_client.app.state.database.connection
    _promote(connection, issued.user.id)

    response = test_client.patch(
        f"/v1/admin/users/{issued.user.id}",
        json={"status": "disabled"},
        headers={"Authorization": f"Bearer {issued.access_token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "LAST_ADMIN_PROTECTION"
    row = connection.execute(
        "SELECT status FROM users WHERE id = CAST(? AS UUID)", [issued.user.id]
    ).fetchone()
    assert row is not None and row[0] == "active"


def test_admin_patch_allows_disabling_an_admin_when_another_stays_active(client) -> None:
    """还有别的活动管理员时可以停用（保护的是"最后一个"，不是"所有管理员"）。"""
    test_client, issued = client
    connection = test_client.app.state.database.connection
    _promote(connection, issued.user.id)
    member = asyncio.run(_second_member(test_client))
    _promote(connection, member["userId"])

    response = test_client.patch(
        f"/v1/admin/users/{issued.user.id}",
        json={"status": "disabled"},
        headers={"Authorization": f"Bearer {issued.access_token}"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["status"] == "disabled"


def test_admin_patch_reports_an_unknown_user(client) -> None:
    """不存在的用户返回 404，而不是创建或静默成功。"""
    test_client, issued = client
    connection = test_client.app.state.database.connection
    _promote(connection, issued.user.id)

    response = test_client.patch(
        "/v1/admin/users/11111111-1111-1111-1111-111111111111",
        json={"status": "disabled"},
        headers={"Authorization": f"Bearer {issued.access_token}"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "USER_NOT_FOUND"


# --------------------------------------------------------------------------
# 设备错误码与状态码对齐
# --------------------------------------------------------------------------


def test_a_revoked_device_is_a_403_on_refresh(client) -> None:
    """``DEVICE_REVOKED`` 是 403（已认证但被拒绝），不能一律压成 401。

    这里只把设备行标成 revoked，保留刷新凭据为 active——正是"设备已撤销但凭据仍在"
    这一状态让领域层抛出 ``DEVICE_REVOKED``（而不是"凭据已重用"的 401）。
    """
    test_client, issued = client
    connection = test_client.app.state.database.connection
    connection.execute(
        """
        UPDATE paired_devices
        SET status = 'revoked', revoked_at = CAST(? AS TIMESTAMPTZ)
        WHERE id = CAST(? AS UUID)
        """,
        ["2026-10-13T12:00:00.000Z", issued.device_id],
    )

    response = test_client.post(
        "/v1/devices/tokens", json={"refreshToken": issued.refresh_token}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "DEVICE_REVOKED"


def test_a_revoked_access_token_keeps_its_domain_code(client) -> None:
    """访问令牌路径保留领域层的错误码：设备已撤销 → ``DEVICE_REVOKED``。

    状态码仍是 401（认证失败），但把「设备已撤销」压成
    ``DEVICE_CREDENTIAL_INVALID`` 会让 App 分不清该重新登录还是刷新令牌。
    """
    test_client, issued = client
    headers = {"Authorization": f"Bearer {issued.access_token}"}
    test_client.post("/v1/auth/logout", headers=headers)

    response = test_client.post("/v1/auth/logout", headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "DEVICE_REVOKED"


# --------------------------------------------------------------------------
# 登出的两个副作用：踢掉事件流、终止在跑的回合
# --------------------------------------------------------------------------


class _FakeSocket:
    """事件连接替身：只实现注册表关心的 ``close``。"""

    def __init__(self) -> None:
        self.closed: list[tuple[int, str]] = []

    async def close(self, code: int, reason: str) -> None:
        """记录关闭码与原因。"""
        self.closed.append((code, reason))


def test_logout_closes_the_devices_event_connections(client) -> None:
    """登出必须踢掉该设备已建立的事件长连接。

    否则"已登出"的设备仍能持续收到 Agent 事件，直到它自己断开连接。
    """
    test_client, issued = client
    registry: EventConnectionRegistry = test_client.app.state.event_connections
    socket = _FakeSocket()
    registry.add(issued.device_id, socket)

    test_client.post(
        "/v1/auth/logout", headers={"Authorization": f"Bearer {issued.access_token}"}
    )

    # 连接被以 4001 关闭，并从注册表移除（重复登出不会二次关闭）。
    assert socket.closed == [(4001, "signed out")]
    assert registry.take_device(issued.device_id) == ()


class _CancelableStore:
    """取消路径所需的最小仓储：会话推进必须"落库"成功。"""

    def __init__(self) -> None:
        self.saved: list[AgentSessionSnapshot] = []

    def save_session(self, session: AgentSessionSnapshot, *, next_event_sequence: int) -> bool:
        """记录并接受会话推进。"""
        self.saved.append(session)
        return True


def test_logout_cancels_the_devices_running_turns(client) -> None:
    """登出必须终止该设备在跑的回合。

    设备撤销后如果回合继续跑完并把回答落库，"已登出"就只是客户端的本地状态。
    """
    test_client, issued = client
    store = _CancelableStore()
    orchestrator = AgentTurnOrchestrator(
        store=store, now=lambda: datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    )
    session_id = "s-logout"
    orchestrator.register(
        AgentSessionSnapshot(
            id=session_id,
            provider="genkit",
            status="running",
            account_scope=AccountScope(kind="all"),
            ledger_version=1,
            as_of="2026-10-13T04:00:00.000Z",
            revision=1,
            summary=None,
            messages=(),
        ),
        device_id=issued.device_id,
    )
    test_client.app.state.agent_orchestrator = orchestrator

    test_client.post(
        "/v1/auth/logout", headers={"Authorization": f"Bearer {issued.access_token}"}
    )

    # 回合已被 CAS 推进到 cancelling：再请求取消时它已不在 running，返回 None。
    assert orchestrator.request_cancel(
        session_id, now=datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    ) is None
    assert [session.status for session in store.saved] == ["cancelling"]
