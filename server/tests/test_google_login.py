"""Google OIDC 登录来源测试。

分两层：
- ``validate_google_login_claims`` 是**纯函数**，逐条覆盖 OIDC 声明校验；
- ``GoogleLoginClient`` 用注入的抓取器与验签入口测试交换流程，不触网。

最重要的一条认识：签名验证由官方库负责，本模块只做官方库不覆盖的边界。
因此测试里注入的"验签入口"直接返回 payload——**它模拟的是已经验签成功的输入**，
而不是在测试里绕过验签。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lyra.application.app_error import ApplicationError
from lyra.infrastructure.auth.google_login_client import (
    GOOGLE_LOGIN_CALLBACK_PATH,
    GoogleLoginClient,
    GoogleLoginConfig,
    assert_configured_owner,
    load_google_login_config,
    matches_hash,
    sha256,
    validate_google_login_claims,
    validate_google_login_config,
)
from lyra.infrastructure.auth.oauth_login_client import OAuthHttpResponse

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
NONCE = "raw-nonce-value"
NONCE_HASH = sha256(NONCE)

CLIENT_ID = "client.apps.googleusercontent.com"
CONFIG = GoogleLoginConfig(
    client_id=CLIENT_ID,
    client_secret="secret",
    redirect_uri=f"https://lyra.example.com{GOOGLE_LOGIN_CALLBACK_PATH}",
    owner_email="owner@example.com",
    owner_subject=None,
)


def _claims(**overrides: object) -> dict[str, object]:
    """构造一组满足全部要求的 ID token 声明。"""
    base: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "exp": int((NOW + timedelta(hours=1)).timestamp()),
        "iat": int(NOW.timestamp()),
        "nonce": NONCE,
        "sub": "112233445566778899000",
        "email_verified": True,
        "email": "Owner@Example.com",
        "name": "  Owner Name  ",
    }
    base.update(overrides)
    return base


def _json_response(payload: object, *, status: int = 200) -> OAuthHttpResponse:
    """构造 JSON 响应。"""
    body = json.dumps(payload).encode("utf-8")
    return OAuthHttpResponse(
        status_code=status,
        headers={"content-type": "application/json", "content-length": str(len(body))},
        body=body,
    )


class Recorder:
    """记录调用的抓取器。"""

    def __init__(self, responses: list[OAuthHttpResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> OAuthHttpResponse:
        self.calls.append({"url": url, **kwargs})
        return self.responses[len(self.calls) - 1]


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


def test_load_returns_none_when_unconfigured() -> None:
    """五项全空时来源未启用。"""
    assert load_google_login_config({}) is None


def test_load_rejects_partial_configuration() -> None:
    """部分配置必须报错。"""
    with pytest.raises(ValueError, match="缺少 LYRA_LOGIN_GOOGLE_CLIENT_SECRET"):
        load_google_login_config({"LYRA_LOGIN_GOOGLE_CLIENT_ID": "cid"})


def test_load_requires_an_owner() -> None:
    """必须配置至少一个 owner 维度——Lyra 不开放注册。"""
    with pytest.raises(ValueError, match="LYRA_OWNER_EMAIL 或 LYRA_OWNER_GOOGLE_SUBJECT"):
        load_google_login_config({
            "LYRA_LOGIN_GOOGLE_CLIENT_ID": "cid",
            "LYRA_LOGIN_GOOGLE_CLIENT_SECRET": "s",
            "LYRA_LOGIN_GOOGLE_REDIRECT_URI": f"https://l.example{GOOGLE_LOGIN_CALLBACK_PATH}",
        })


def test_load_normalizes_owner_email_case() -> None:
    """owner 邮箱统一小写保存，避免大小写造成准入差异。"""
    config = load_google_login_config({
        "LYRA_LOGIN_GOOGLE_CLIENT_ID": "cid",
        "LYRA_LOGIN_GOOGLE_CLIENT_SECRET": "s",
        "LYRA_LOGIN_GOOGLE_REDIRECT_URI": f"https://l.example{GOOGLE_LOGIN_CALLBACK_PATH}",
        "LYRA_OWNER_EMAIL": " Owner@Example.COM ",
    })
    assert config is not None
    assert config.owner_email == "owner@example.com"


@pytest.mark.parametrize(
    "redirect_uri",
    [
        f"http://lyra.example.com{GOOGLE_LOGIN_CALLBACK_PATH}",
        "https://lyra.example.com/v1/auth/wrong/callback",
        f"https://u:p@lyra.example.com{GOOGLE_LOGIN_CALLBACK_PATH}",
        f"https://lyra.example.com{GOOGLE_LOGIN_CALLBACK_PATH}?x=1",
    ],
)
def test_validate_rejects_non_fixed_callback(redirect_uri: str) -> None:
    """回调地址必须是固定 HTTPS 路径。"""
    with pytest.raises(ValueError, match="固定 HTTPS"):
        validate_google_login_config(replace(CONFIG, redirect_uri=redirect_uri))


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "@example.com", "a b@example.com"])
def test_validate_rejects_malformed_owner_email(email: str) -> None:
    """owner 邮箱格式必须合法。"""
    with pytest.raises(ValueError, match="LYRA_OWNER_EMAIL 格式无效"):
        validate_google_login_config(replace(CONFIG, owner_email=email))


# --------------------------------------------------------------------------
# OIDC 声明校验
# --------------------------------------------------------------------------


def test_accepts_valid_claims_and_normalizes_fields() -> None:
    """合法声明应通过，并对邮箱与展示名做规范化。"""
    identity = validate_google_login_claims(_claims(), audience=CLIENT_ID, nonce_hash=NONCE_HASH, now=NOW)

    assert identity.provider == "google"
    assert identity.subject == "112233445566778899000"
    assert identity.email == "owner@example.com"
    assert identity.display_name == "Owner Name"


@pytest.mark.parametrize("issuer", ["https://accounts.google.com", "accounts.google.com"])
def test_accepts_both_google_issuer_forms(issuer: str) -> None:
    """Google 同时使用带协议与不带协议两种 issuer。"""
    identity = validate_google_login_claims(
        _claims(iss=issuer), audience=CLIENT_ID, nonce_hash=NONCE_HASH, now=NOW
    )
    assert identity.subject


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"iss": "https://evil.example.com"}, "issuer 不符"),
        ({"aud": "other-client"}, "audience 不符"),
        ({"azp": "other-client"}, "azp 与 audience 不符"),
        ({"exp": int(NOW.timestamp()) - 1}, "已过期"),
        ({"exp": "soon"}, "exp 不是整数"),
        ({"iat": int((NOW + timedelta(minutes=5)).timestamp())}, "iat 超前过多"),
        ({"iat": "now"}, "iat 不是整数"),
        # exp 与 iat 都在有效期内，但生命周期为负——精确触发 exp <= iat 分支。
        (
            {
                "iat": int((NOW + timedelta(seconds=30)).timestamp()),
                "exp": int((NOW + timedelta(seconds=20)).timestamp()),
            },
            "exp 不晚于 iat",
        ),
        ({"nonce": "different-nonce"}, "nonce 不匹配"),
        ({"sub": ""}, "sub 为空"),
        ({"sub": "has space"}, "sub 含空白"),
        ({"email_verified": False}, "邮箱未验证"),
        ({"email_verified": "true"}, "email_verified 不是布尔"),
        ({"email": "bad"}, "邮箱格式无效"),
    ],
)
def test_rejects_invalid_claims(overrides: dict[str, object], reason: str) -> None:
    """任一声明不满足要求都必须折叠为统一的登录失败。"""
    with pytest.raises(ApplicationError) as raised:
        validate_google_login_claims(
            _claims(**overrides), audience=CLIENT_ID, nonce_hash=NONCE_HASH, now=NOW
        )
    assert raised.value.code == "LOGIN_FAILED", reason
    assert raised.value.status_code == 401


def test_rejects_non_dict_claims() -> None:
    """非对象 payload 必须拒绝。"""
    with pytest.raises(ApplicationError):
        validate_google_login_claims(["x"], audience=CLIENT_ID, nonce_hash=NONCE_HASH, now=NOW)


def test_matches_hash_is_strict_about_hash_format() -> None:
    """哈希格式非法一律不匹配，而不是抛异常。"""
    assert matches_hash(NONCE, NONCE_HASH) is True
    assert matches_hash(NONCE, "not-a-hash") is False
    assert matches_hash(NONCE, "A" * 64) is False
    assert matches_hash("other", NONCE_HASH) is False


def test_display_name_is_optional() -> None:
    """没有 name 或 name 为空白时展示名为空。"""
    assert validate_google_login_claims(
        _claims(name="   "), audience=CLIENT_ID, nonce_hash=NONCE_HASH, now=NOW
    ).display_name is None
    assert validate_google_login_claims(
        _claims(name=123), audience=CLIENT_ID, nonce_hash=NONCE_HASH, now=NOW
    ).display_name is None


def test_display_name_is_truncated() -> None:
    """展示名截断到 256 字符。"""
    identity = validate_google_login_claims(
        _claims(name="x" * 300), audience=CLIENT_ID, nonce_hash=NONCE_HASH, now=NOW
    )
    assert identity.display_name is not None
    assert len(identity.display_name) == 256


# --------------------------------------------------------------------------
# owner 准入
# --------------------------------------------------------------------------


def _identity(subject: str = "112233445566778899000", email: str = "owner@example.com"):
    from lyra.infrastructure.auth.google_login_client import GoogleLoginIdentity

    return GoogleLoginIdentity(provider="google", subject=subject, email=email, display_name=None)


def test_owner_accepts_matching_email() -> None:
    """邮箱匹配即通过（未配置 subject 时）。"""
    assert_configured_owner(_identity(), CONFIG)


def test_owner_rejects_other_email() -> None:
    """其它邮箱必须被拒。"""
    with pytest.raises(ApplicationError) as raised:
        assert_configured_owner(_identity(email="other@example.com"), CONFIG)
    assert raised.value.code == "LOGIN_DENIED"
    assert raised.value.status_code == 403


def test_owner_requires_every_configured_dimension() -> None:
    """**配置了的每个维度都必须匹配**，不能只满足其一。"""
    both = replace(CONFIG, owner_subject="112233445566778899000")
    assert_configured_owner(_identity(), both)

    # subject 对但邮箱错 → 拒绝。
    with pytest.raises(ApplicationError):
        assert_configured_owner(_identity(email="other@example.com"), both)
    # 邮箱对但 subject 错 → 拒绝。
    with pytest.raises(ApplicationError):
        assert_configured_owner(_identity(subject="9"), both)


# --------------------------------------------------------------------------
# 交换流程
# --------------------------------------------------------------------------


def test_authorization_url_includes_nonce_pkce_and_account_prompt() -> None:
    """授权地址必须带 nonce、S256 PKCE 与账号选择提示。"""
    url = GoogleLoginClient(CONFIG).authorization_url(
        state="st", nonce="n", code_challenge="challenge"
    )

    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "nonce=n" in url
    assert "code_challenge=challenge" in url
    assert "code_challenge_method=S256" in url
    assert "prompt=select_account" in url
    assert "scope=openid+email+profile" in url


def test_exchange_verifies_id_token_and_returns_identity() -> None:
    """交换成功后必须用验签入口校验 ID token 并返回身份。"""
    fetcher = Recorder([_json_response({"id_token": "header.payload.signature"})])
    verified: list[str] = []

    def verify(id_token: str) -> object:
        verified.append(id_token)
        return _claims()

    identity = GoogleLoginClient(CONFIG, fetcher, verify).exchange_code(
        code="code", code_verifier="verifier", nonce_hash=NONCE_HASH, now=NOW
    )

    assert verified == ["header.payload.signature"]
    assert identity.email == "owner@example.com"
    assert fetcher.calls[0]["url"] == "https://oauth2.googleapis.com/token"
    assert b"code_verifier=verifier" in fetcher.calls[0]["body"]


def test_exchange_folds_failures_without_leaking_secrets() -> None:
    """失败必须折叠为统一安全错误，不泄漏授权码、密钥或 ID token。"""
    cases = [
        ([_json_response({"error": "invalid_grant"})], lambda _: _claims()),
        ([_json_response({"id_token": ""})], lambda _: _claims()),
        ([_json_response({"id_token": "t"})], lambda _: _claims(aud="other")),
        ([_json_response({"id_token": "t"}, status=500)], lambda _: _claims()),
    ]
    for responses, verify in cases:
        client = GoogleLoginClient(CONFIG, Recorder(responses), verify)
        with pytest.raises(ApplicationError) as raised:
            client.exchange_code(
                code="SECRET-CODE", code_verifier="v", nonce_hash=NONCE_HASH, now=NOW
            )
        assert raised.value.code == "LOGIN_FAILED"
        assert "SECRET-CODE" not in raised.value.message
        assert "secret" not in raised.value.message
        assert "header.payload" not in raised.value.message


def test_exchange_folds_verifier_exception() -> None:
    """官方验签抛出异常时同样折叠为安全错误。"""
    def verify(_: str) -> object:
        raise RuntimeError("bad signature for token header.payload.signature")

    client = GoogleLoginClient(CONFIG, Recorder([_json_response({"id_token": "t"})]), verify)
    with pytest.raises(ApplicationError) as raised:
        client.exchange_code(code="c", code_verifier="v", nonce_hash=NONCE_HASH, now=NOW)
    assert raised.value.code == "LOGIN_FAILED"
    assert "bad signature" not in raised.value.message
