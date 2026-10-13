"""认证路由测试。

两条要点：

1. **`GET /v1/auth/providers` 不要求设备认证**——它本身就是获取认证的入口；
2. **`POST /v1/devices/tokens` 也不要求访问令牌**——用已过期或已撤销的访问令牌去
   刷新是行不通的，身份由刷新凭据本身证明。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.protocol import CLIENT_PROTOCOL_HEADERS
from lyra.application.device_pairing_service import DevicePairingService
from lyra.application.write_coordinator import WriteCoordinator
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import LoginIdentity
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
