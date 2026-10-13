"""普通 OAuth 登录来源测试（GitHub / LINUX DO）。

用可注入的抓取器替代真实网络，因此这些测试是**确定性的**，同时保留了真实
实现的安全约束：固定端点、不传播底层异常、响应体积上限、身份字段严格校验。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from lyra.application.app_error import ApplicationError
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.auth.oauth_login_client import (
    EXCHANGE_TIMEOUT_SECONDS,
    GITHUB_LOGIN_CALLBACK_PATH,
    LINUXDO_LOGIN_CALLBACK_PATH,
    OAuthHttpResponse,
    OAuthLoginClient,
    OAuthLoginConfig,
    assert_oauth_configured_owner,
    identity_from_profile,
    is_canonical_subject,
    load_oauth_login_config,
    read_bounded_json,
    validate_oauth_login_config,
)

GITHUB = OAuthLoginConfig(
    provider="github",
    client_id="cid",
    client_secret="secret",
    redirect_uri=f"https://lyra.example.com{GITHUB_LOGIN_CALLBACK_PATH}",
    owner_subject="12345",
)
LINUXDO = OAuthLoginConfig(
    provider="linuxdo",
    client_id="cid",
    client_secret="secret",
    redirect_uri=f"https://lyra.example.com{LINUXDO_LOGIN_CALLBACK_PATH}",
    owner_subject="67890",
)


class Recorder:
    """记录调用并按预设脚本返回响应的抓取器。"""

    def __init__(self, responses: list[OAuthHttpResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> OAuthHttpResponse:
        self.calls.append({
            "url": url,
            "method": method,
            "headers": dict(headers),
            "body": body,
            "timeout": timeout_seconds,
        })
        return self.responses[len(self.calls) - 1]


def _json_response(payload: object, *, status: int = 200) -> OAuthHttpResponse:
    """构造一个 JSON 响应。"""
    body = json.dumps(payload).encode("utf-8")
    return OAuthHttpResponse(
        status_code=status,
        headers={"content-type": "application/json", "content-length": str(len(body))},
        body=body,
    )


def _token_payload(token: str = "gho_abcdef123456") -> dict[str, object]:
    return {"access_token": token, "token_type": "bearer"}


# --------------------------------------------------------------------------
# 配置加载与校验
# --------------------------------------------------------------------------


def test_load_returns_none_when_fully_unconfigured() -> None:
    """四项全空时关闭来源。"""
    assert load_oauth_login_config({}, "github") is None
    assert load_oauth_login_config({"LYRA_LOGIN_GITHUB_CLIENT_ID": "  "}, "github") is None


def test_load_rejects_partial_configuration() -> None:
    """部分配置必须报错，不能静默当成未配置。"""
    with pytest.raises(ValueError, match="缺少 LYRA_LOGIN_GITHUB_CLIENT_SECRET"):
        load_oauth_login_config({"LYRA_LOGIN_GITHUB_CLIENT_ID": "cid"}, "github")


def test_load_accepts_complete_configuration() -> None:
    """完整配置应通过并去除两端空白。"""
    config = load_oauth_login_config(
        {
            "LYRA_LOGIN_LINUXDO_CLIENT_ID": " cid ",
            "LYRA_LOGIN_LINUXDO_CLIENT_SECRET": "secret",
            "LYRA_LOGIN_LINUXDO_REDIRECT_URI": f"https://l.example{LINUXDO_LOGIN_CALLBACK_PATH}",
            "LYRA_OWNER_LINUXDO_SUBJECT": "67890",
        },
        "linuxdo",
    )
    assert config is not None
    assert config.client_id == "cid"
    assert config.owner_subject == "67890"


@pytest.mark.parametrize(
    "redirect_uri",
    [
        f"http://lyra.example.com{GITHUB_LOGIN_CALLBACK_PATH}",  # 公网 HTTP
        "https://lyra.example.com/v1/auth/wrong/callback",  # 路径不符
        f"https://user:pw@lyra.example.com{GITHUB_LOGIN_CALLBACK_PATH}",  # 携带凭据
        f"https://lyra.example.com{GITHUB_LOGIN_CALLBACK_PATH}?x=1",  # 含查询串
        f"https://lyra.example.com{GITHUB_LOGIN_CALLBACK_PATH}#frag",  # 含片段
    ],
)
def test_validate_rejects_non_fixed_callback(redirect_uri: str) -> None:
    """回调地址必须是固定的 HTTPS 路径，无凭据、无查询串、无片段。"""
    with pytest.raises(ValueError, match="固定 HTTPS"):
        validate_oauth_login_config(replace(GITHUB, redirect_uri=redirect_uri))


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_validate_allows_loopback_http(host: str) -> None:
    """本机开发允许 loopback HTTP。"""
    validate_oauth_login_config(
        replace(GITHUB, redirect_uri=f"http://{host}:3141{GITHUB_LOGIN_CALLBACK_PATH}")
    )


@pytest.mark.parametrize("subject", ["", "0", "01", "12345678901234567", "abc", "-1", "1.0"])
def test_validate_rejects_non_canonical_owner_subject(subject: str) -> None:
    """owner ID 必须是规范的正整数文本，不接受前导零或溢出值。"""
    with pytest.raises(ValueError, match="正整数用户 ID"):
        validate_oauth_login_config(replace(GITHUB, owner_subject=subject))
    assert is_canonical_subject(subject) is False


def test_is_canonical_subject_accepts_plain_ids() -> None:
    """普通正整数文本应通过。"""
    assert is_canonical_subject("1") is True
    assert is_canonical_subject("12345") is True


@pytest.mark.parametrize("credential", ["", "   ", "a" * 4097, "bad\x00value"])
def test_validate_rejects_invalid_credentials(credential: str) -> None:
    """凭据必须非空、长度受限且不含控制字符。"""
    with pytest.raises(ValueError, match="有效的 OAuth client ID"):
        validate_oauth_login_config(replace(GITHUB, client_id=credential))


# --------------------------------------------------------------------------
# owner 准入
# --------------------------------------------------------------------------


def test_owner_assertion_accepts_matching_subject() -> None:
    """provider 与 subject 都匹配时通过。"""
    assert_oauth_configured_owner(
        LoginIdentity(provider="github", subject="12345", email=None, display_name=None), GITHUB
    )


def test_owner_assertion_rejects_other_provider_with_same_subject() -> None:
    """另一个来源即使 subject 相同也不得获得权限。"""
    with pytest.raises(ApplicationError) as raised:
        assert_oauth_configured_owner(
            LoginIdentity(provider="linuxdo", subject="12345", email=None, display_name=None),
            GITHUB,
        )
    assert raised.value.code == "LOGIN_DENIED"
    assert raised.value.status_code == 403


def test_owner_assertion_never_uses_email() -> None:
    """邮箱相同不构成准入依据。"""
    with pytest.raises(ApplicationError):
        assert_oauth_configured_owner(
            LoginIdentity(
                provider="github",
                subject="99999",
                email="owner@example.com",
                display_name="owner",
            ),
            GITHUB,
        )


# --------------------------------------------------------------------------
# 授权地址
# --------------------------------------------------------------------------


def test_github_authorization_url_includes_s256_pkce() -> None:
    """GitHub 必须带上 S256 PKCE 挑战。"""
    url = OAuthLoginClient(GITHUB).authorization_url(
        state="st", nonce="n", code_challenge="challenge"
    )

    assert url.startswith("https://github.com/login/oauth/authorize?")
    assert "code_challenge=challenge" in url
    assert "code_challenge_method=S256" in url
    assert "state=st" in url
    assert "scope=read%3Auser" in url


def test_linuxdo_authorization_url_omits_pkce() -> None:
    """LINUX DO 尚无官方 PKCE 支持保证，不发送 PKCE 参数。"""
    url = OAuthLoginClient(LINUXDO).authorization_url(
        state="st", nonce="n", code_challenge="challenge"
    )

    assert url.startswith("https://connect.linux.do/oauth2/authorize?")
    assert "code_challenge" not in url
    assert "scope=user" in url


# --------------------------------------------------------------------------
# 交换流程
# --------------------------------------------------------------------------


def test_github_exchange_returns_identity() -> None:
    """成功交换应返回来自固定用户端点的身份。"""
    fetcher = Recorder([
        _json_response(_token_payload()),
        _json_response({"id": 12345, "login": "octocat", "name": "The Octocat"}),
    ])

    identity = OAuthLoginClient(GITHUB, fetcher).exchange_code(
        code="code", code_verifier="verifier", nonce_hash="h", now=None
    )

    assert identity.provider == "github"
    assert identity.subject == "12345"
    assert identity.display_name == "The Octocat"
    # 只做身份登录，不申请也不推断邮箱。
    assert identity.email is None


def test_github_exchange_targets_fixed_endpoints_only() -> None:
    """端点必须来自服务端常量，且携带 PKCE verifier。"""
    fetcher = Recorder([
        _json_response(_token_payload()),
        _json_response({"id": 1, "login": "x"}),
    ])

    OAuthLoginClient(GITHUB, fetcher).exchange_code(
        code="code", code_verifier="verifier", nonce_hash="h", now=None
    )

    assert fetcher.calls[0]["url"] == "https://github.com/login/oauth/access_token"
    assert fetcher.calls[1]["url"] == "https://api.github.com/user"
    assert b"code_verifier=verifier" in (fetcher.calls[0]["body"] or b"")
    assert fetcher.calls[0]["timeout"] == EXCHANGE_TIMEOUT_SECONDS


def test_linuxdo_exchange_omits_verifier() -> None:
    """LINUX DO 不发送 PKCE verifier。"""
    fetcher = Recorder([
        _json_response(_token_payload("ldo_token")),
        _json_response({"id": 67890, "username": "linuxdo-user", "active": True}),
    ])

    identity = OAuthLoginClient(LINUXDO, fetcher).exchange_code(
        code="code", code_verifier="verifier", nonce_hash="h", now=None
    )

    assert b"code_verifier" not in (fetcher.calls[0]["body"] or b"")
    assert identity.subject == "67890"
    assert identity.display_name == "linuxdo-user"


def test_exchange_folds_every_failure_into_safe_error() -> None:
    """失败必须折叠成同一个安全错误，且不泄漏授权码或密钥。"""
    for responses in (
        [_json_response({"error": "bad_verification_code"})],
        [_json_response(_token_payload(), status=500)],
        [_json_response({"access_token": "tok", "token_type": "mac"})],
        [_json_response(_token_payload()), _json_response({"message": "Bad credentials"})],
        [_json_response(_token_payload()), _json_response({"id": 0})],
    ):
        client = OAuthLoginClient(GITHUB, Recorder(responses))
        with pytest.raises(ApplicationError) as raised:
            client.exchange_code(code="SECRET-CODE", code_verifier="v", nonce_hash="h", now=None)
        assert raised.value.code == "LOGIN_FAILED"
        assert raised.value.status_code == 401
        assert "SECRET-CODE" not in raised.value.message
        assert "secret" not in raised.value.message


def test_exchange_rejects_redirected_response() -> None:
    """发生重定向即视为异常响应，不跟随也不解析。"""
    redirected = OAuthHttpResponse(
        status_code=200, headers={}, body=b'{"access_token":"t","token_type":"bearer"}',
        redirected=True,
    )
    client = OAuthLoginClient(GITHUB, Recorder([redirected]))
    with pytest.raises(ApplicationError):
        client.exchange_code(code="c", code_verifier="v", nonce_hash="h", now=None)


def test_exchange_rejects_oversized_declared_length() -> None:
    """声明长度超限必须拒绝，不读取正文。"""
    oversized = OAuthHttpResponse(
        status_code=200, headers={"content-length": str(1024 * 1024)}, body=b"{}"
    )
    with pytest.raises(ValueError, match="Invalid OAuth HTTP response"):
        read_bounded_json(oversized)


def test_exchange_rejects_oversized_actual_body() -> None:
    """**实际**字节数超限也必须拒绝——不能只信任 Content-Length。"""
    oversized = OAuthHttpResponse(
        status_code=200, headers={}, body=b"x" * (64 * 1024 + 1)
    )
    with pytest.raises(ValueError, match="Invalid OAuth HTTP response"):
        read_bounded_json(oversized)


def test_exchange_rejects_non_json_body() -> None:
    """非 UTF-8 或非 JSON 正文必须拒绝。"""
    for body in (b"\xff\xfe", b"not json"):
        with pytest.raises(ValueError, match="Invalid OAuth HTTP response"):
            read_bounded_json(
                OAuthHttpResponse(status_code=200, headers={}, body=body)
            )


# --------------------------------------------------------------------------
# 身份提取
# --------------------------------------------------------------------------


def test_identity_strips_control_characters_from_display_name() -> None:
    """展示名必须清洗控制字符并截断到 256。"""
    identity = identity_from_profile({"id": 7, "name": "a\x00b\x1fc"}, "github")
    assert identity.display_name == "abc"

    long_name = identity_from_profile({"id": 7, "name": "x" * 300}, "github")
    assert long_name.display_name is not None
    assert len(long_name.display_name) == 256


def test_identity_falls_back_to_login_or_username() -> None:
    """没有 name 时回落到 login / username。"""
    assert identity_from_profile({"id": 7, "login": "octocat"}, "github").display_name == "octocat"
    assert identity_from_profile(
        {"id": 7, "username": "ldo"}, "linuxdo"
    ).display_name == "ldo"


@pytest.mark.parametrize(
    "profile",
    [
        {"id": "7"},           # 数字 ID 不能是字符串
        {"id": 0},             # 必须是正数
        {"id": -1},
        {"id": True},          # 布尔不是 ID
        {"error": "x", "id": 7},
        {"login": "x"},        # 缺 ID
    ],
)
def test_identity_rejects_invalid_id(profile: dict[str, object]) -> None:
    """身份 ID 必须是非布尔的正整数。"""
    with pytest.raises(ValueError, match="Invalid OAuth user identity"):
        identity_from_profile(profile, "github")


def test_identity_rejects_deactivated_linuxdo_account() -> None:
    """LINUX DO 的 active=false 必须拒绝。"""
    with pytest.raises(ValueError, match="Invalid OAuth account status"):
        identity_from_profile({"id": 7, "active": False}, "linuxdo")


def test_identity_allows_silenced_linuxdo_account() -> None:
    """silenced 只是论坛禁言，不代表第三方登录被禁用。"""
    identity = identity_from_profile({"id": 7, "active": True, "silenced": True}, "linuxdo")
    assert identity.subject == "7"
