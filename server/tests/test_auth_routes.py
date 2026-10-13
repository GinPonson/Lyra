"""认证路由测试。

三条要点：

1. **`GET /v1/auth/providers` 不要求设备认证**——它本身就是获取认证的入口；
2. **`POST /v1/devices/tokens` 也不要求访问令牌**——用已过期或已撤销的访问令牌去
   刷新是行不通的，身份由刷新凭据本身证明；
3. **正文类型收口在路由层**——非字符串字段必须 400，不能穿透到服务层变成 500。
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
from lyra.application.owner_login_service import OwnerLoginService
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
from lyra.main import create_app

PUBLIC_KEY_PEM = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"][0]["inputPem"]
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
    service = DevicePairingService(
        DuckDbDeviceRepository(database.connection), WriteCoordinator()
    )
    issued = asyncio.run(service.authorize_owner_device(
        identity=IDENTITY, display_name="Pixel 9", public_key_pem=PUBLIC_KEY_PEM
    ))
    try:
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
            yield test_client, issued
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
            DuckDbDeviceRepository(connection), WriteCoordinator()
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


def test_callback_returns_html_without_tokens(client) -> None:
    """**回调页面不含任何令牌**：浏览器只完成验证，凭据经 App 的 POST 返回。"""
    test_client, _ = client
    response = test_client.get(
        "/v1/auth/github/callback", params={"state": "s" * 43, "code": "c"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    for leaked in ("token", "accessToken", "refreshToken", "code=", "s" * 43):
        assert leaked not in body, leaked


def test_callback_page_is_identical_for_success_and_failure(client) -> None:
    """**成功与失败返回同一页面。**

    区分会让未授权方凭页面内容判断某个 state 是否存在。
    """
    test_client, _ = client
    unknown = test_client.get(
        "/v1/auth/github/callback", params={"state": "A" * 43, "code": "c"}
    ).text
    denied = test_client.get(
        "/v1/auth/github/callback", params={"state": "A" * 43, "error": "access_denied"}
    ).text

    assert unknown == denied


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


def test_callback_sets_the_full_security_header_set(client) -> None:
    """**回调页面必须带全套安全响应头。**

    它是返回给浏览器的 HTML，而这些头各自挡住一类真实攻击：

    | 头 | 缺了会怎样 |
    | --- | --- |
    | `content-security-policy` 的 `frame-ancestors 'none'` | 页面可被套框（点击劫持） |
    | `x-content-type-options: nosniff` | 浏览器可 MIME 嗅探，把响应当别的类型执行 |
    | `cache-control: no-store` | "一次性"的回调页留在缓存里 |
    | `referrer-policy: no-referrer` | state 随 Referer 泄漏给第三方 |

    参考实现逐项设置了它们；这里用一条测试固定，避免日后被当成冗余删掉。
    """
    test_client, _ = client
    response = test_client.get(
        "/v1/auth/github/callback", params={"state": "A" * 43, "code": "c"}
    )

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "default-src 'none'" in csp


def test_callback_security_headers_apply_to_the_failure_page_too(client) -> None:
    """失败路径同样带头——**两条路径返回的是同一个响应构造器**。"""
    test_client, _ = client
    response = test_client.get(
        "/v1/auth/github/callback", params={"state": "A" * 43, "error": "access_denied"}
    )

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
    test_client.app.state.login_service = OwnerLoginService(
        [
            ConfiguredLoginProvider(
                id="github",
                name="GitHub",
                client=_FakeLoginClient(),
                assert_owner=lambda identity: None,
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
# 兑换响应：必须带 user.email
# --------------------------------------------------------------------------


class _CompletingLoginService:
    """固定返回一次已完成兑换的登录服务；只用于验证响应组装。"""

    def __init__(self, identity: LoginIdentity) -> None:
        self._identity = identity

    async def exchange(
        self, provider_id: str, *, login_id: str, code_verifier: str
    ) -> dict[str, Any]:
        """返回一份完整的兑换结果。"""
        return {
            "status": "complete",
            "deviceId": "device-1",
            "displayName": "Pixel 9",
            "accessToken": "access",
            "accessTokenExpiresAt": "2026-10-13T00:10:00.000Z",
            "refreshToken": "refresh",
            "refreshTokenExpiresAt": "2026-11-12T00:00:00.000Z",
            "user": self._identity,
        }


def test_exchange_returns_the_user_email(client) -> None:
    """``user.email`` 必须出现在响应里。

    客户端把它展示在个人资料页（``user?['email']``）；丢了它 App 会静默显示空邮箱。
    """
    test_client, _ = client
    test_client.app.state.login_service = _CompletingLoginService(
        LoginIdentity(
            provider="github", subject="12345", email="owner@example.com", display_name="octocat"
        )
    )

    response = test_client.post(
        "/v1/auth/github/exchange",
        json={"loginId": "11111111-1111-1111-1111-111111111111", "codeVerifier": "v" * 43},
    )

    assert response.status_code == 200
    assert response.json()["user"] == {
        "provider": "github",
        "subject": "12345",
        "email": "owner@example.com",
        "displayName": "octocat",
    }


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
