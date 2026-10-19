"""Google OAuth 授权请求构造测试。

最要紧的一条：**``access_type=offline`` 与 ``prompt=consent`` 必须成对出现。**

缺任何一个，Google 都不会返回 refresh token，于是连接在 access token 过期后
**再也无法刷新**——用户会看到"连接突然失效"，重新授权一次又短暂可用，如此反复。
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from lyra.domain.gmail.sync_plan import GmailSyncError
from lyra.infrastructure.gmail.oauth_transaction import (
    GMAIL_READONLY_SCOPE,
    GmailOAuthError,
    create_google_oauth_transaction,
)
from lyra.infrastructure.gmail.google_oauth_client import (
    GOOGLE_AUTHORIZATION_ENDPOINT,
    GOOGLE_GMAIL_AUTHORIZATION_SCOPES,
    GOOGLE_OAUTH_TIMEOUT_MILLISECONDS,
    GOOGLE_TOKEN_ENDPOINT,
    GOOGLE_TOKEN_REVOCATION_ENDPOINT,
    GOOGLE_USERINFO_ENDPOINT,
    GoogleEndpointFailure,
    build_google_authorization_request,
    build_token_exchange_form,
    build_token_refresh_form,
    calculate_access_token_expiry,
    decrypt_connection_refresh_token,
    exchange_google_authorization_code,
    google_endpoint_error,
    parse_google_token_response,
    parse_verified_identity,
    refresh_google_access_token,
    revoke_gmail_connection_authorization,
    token_endpoint_failure_code,
)

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
REDIRECT = "https://lyra.example.com/v1/auth/google/callback"


def _transaction():
    return create_google_oauth_transaction(
        "device-1", NOW, 10 * 60 * 1000,
        user_id="user-1", authorization_id="authorization-1",
    )


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def _build(**overrides):
    transaction = overrides.pop("transaction", None) or _transaction()
    return build_google_authorization_request(
        transaction,
        client_id=overrides.pop("client_id", "client-1.apps.googleusercontent.com"),
        redirect_uri=overrides.pop("redirect_uri", REDIRECT),
    )


# --------------------------------------------------------------------------
# 端点常量
# --------------------------------------------------------------------------


def test_endpoint_constants_are_the_documented_google_urls() -> None:
    """端点常量为 Google 的正式地址。"""
    assert GOOGLE_TOKEN_ENDPOINT == "https://oauth2.googleapis.com/token"
    assert GOOGLE_USERINFO_ENDPOINT == (
        "https://openidconnect.googleapis.com/v1/userinfo"
    )
    assert GOOGLE_TOKEN_REVOCATION_ENDPOINT == "https://oauth2.googleapis.com/revoke"
    assert GOOGLE_AUTHORIZATION_ENDPOINT == "https://accounts.google.com/o/oauth2/v2/auth"
    assert GOOGLE_OAUTH_TIMEOUT_MILLISECONDS == 15_000


def test_authorization_scopes_include_openid_email_and_readonly() -> None:
    """作用域是 openid + email + Gmail 只读，顺序固定。"""
    assert GOOGLE_GMAIL_AUTHORIZATION_SCOPES == (
        "openid", "email", GMAIL_READONLY_SCOPE,
    )


# --------------------------------------------------------------------------
# 授权地址
# --------------------------------------------------------------------------


def test_builds_the_authorization_url() -> None:
    """构造出的地址带全部必需参数。"""
    transaction = _transaction()

    request = _build(transaction=transaction)
    params = _params(request.authorization_url)

    assert request.authorization_url.startswith(GOOGLE_AUTHORIZATION_ENDPOINT + "?")
    assert params["client_id"] == ["client-1.apps.googleusercontent.com"]
    assert params["redirect_uri"] == [REDIRECT]
    assert params["response_type"] == ["code"]
    assert params["state"] == [transaction.state]
    assert params["code_challenge"] == [transaction.code_challenge]
    assert params["code_challenge_method"] == ["S256"]


def test_offline_access_and_consent_are_both_present() -> None:
    """**``access_type=offline`` 与 ``prompt=consent`` 成对出现。**

    缺任何一个，Google 都不会返回 refresh token，连接在 access token 过期后
    再也无法刷新。
    """
    params = _params(_build().authorization_url)

    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]


def test_incremental_authorization_is_requested() -> None:
    """请求增量授权：保留用户此前授予的其它权限。"""
    params = _params(_build().authorization_url)
    assert params["include_granted_scopes"] == ["true"]


def test_scopes_are_space_separated() -> None:
    """作用域以空格分隔（URL 编码后是 ``+``）。"""
    params = _params(_build().authorization_url)

    assert params["scope"] == [" ".join(GOOGLE_GMAIL_AUTHORIZATION_SCOPES)]


def test_expiry_comes_from_the_transaction() -> None:
    """过期时间取自事务，而不是调用时重新计算。"""
    transaction = _transaction()

    assert _build(transaction=transaction).expires_at == (
        transaction.record.expires_at
    )


def test_url_contains_no_client_secret() -> None:
    """**授权地址不含密钥**——它会被写进浏览器历史与日志。"""
    url = _build().authorization_url

    for leaked in ("client_secret", "secret", "api_key", "token"):
        assert leaked not in url


# --------------------------------------------------------------------------
# 参数校验
# --------------------------------------------------------------------------


@pytest.mark.parametrize("client_id", ["", "   "])
def test_blank_client_id_is_rejected(client_id: str) -> None:
    """client ID 不能为空。"""
    with pytest.raises(ValueError, match="client ID不能为空"):
        _build(client_id=client_id)


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "not-a-url",
        "https://user:pass@lyra.example.com/callback",   # 凭据会随地址外泄
        "https://lyra.example.com/callback#frag",        # fragment 会被浏览器剥离
        "",
    ],
)
def test_invalid_redirect_uris_are_rejected(redirect_uri: str) -> None:
    """**回调地址不得含凭据或 fragment。**"""
    with pytest.raises(ValueError):
        _build(redirect_uri=redirect_uri)


def test_redirect_uri_with_only_a_fragment_is_rejected() -> None:
    """只带 fragment 的地址同样拒绝。"""
    with pytest.raises(ValueError, match="凭据或 fragment"):
        _build(redirect_uri="https://lyra.example.com/cb#x")


# --------------------------------------------------------------------------
# 事务校验
# --------------------------------------------------------------------------


def test_transaction_state_must_be_43_url_safe_characters() -> None:
    """**state 必须是 43 个 URL 安全字符**（32 字节 base64url）。"""
    from dataclasses import replace

    transaction = _transaction()
    broken = replace(transaction, state="too-short")

    with pytest.raises(ValueError, match="state 无效"):
        _build(transaction=broken)


def test_pkce_method_must_be_s256() -> None:
    """**PKCE 方法必须是 S256**；``plain`` 会让 verifier 暴露在授权地址里。"""
    from dataclasses import replace

    transaction = _transaction()
    broken = replace(transaction, code_challenge_method="plain")

    with pytest.raises(ValueError, match="PKCE challenge 无效"):
        _build(transaction=broken)


def test_code_challenge_must_be_43_url_safe_characters() -> None:
    """challenge 形状必须正确。"""
    from dataclasses import replace

    transaction = _transaction()
    broken = replace(transaction, code_challenge="bad!")

    with pytest.raises(ValueError, match="PKCE challenge 无效"):
        _build(transaction=broken)


def test_unparseable_expiry_is_rejected() -> None:
    """过期时间不可解析时必须报错，而不是产出一个立即失效的授权地址。

    **函数名不能叫 ``test_invalid_expiry_is_rejected``**：下面还有一个同名参数化测试
    （令牌 ``expires_in`` 校验），同名的后定义会把这条整个遮蔽掉——它此前从未运行过。
    """
    from dataclasses import replace

    transaction = _transaction()
    broken = replace(
        transaction,
        record=replace(transaction.record, expires_at="not-a-timestamp"),
    )

    with pytest.raises(ValueError, match="过期时间无效"):
        _build(transaction=broken)


def test_state_and_challenge_are_not_confused() -> None:
    """state 与 challenge 各自独立出现在地址里。"""
    transaction = _transaction()
    params = _params(_build(transaction=transaction).authorization_url)

    assert params["state"] == [transaction.state]
    assert params["code_challenge"] == [transaction.code_challenge]
    assert transaction.state != transaction.code_challenge


# --------------------------------------------------------------------------
# 令牌端点失败映射
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403])
def test_revoked_credentials_map_to_auth_revoked(status: int) -> None:
    """**400 / 401 / 403 表示授权已失效**，需要用户重新授权。

    Google 用 400（``invalid_grant``）表示 refresh token 已被撤销/过期。
    把它当成暂时性故障会让客户端对着一个已作废的凭据**无限重试**，
    而每次重试都不会成功。
    """
    assert token_endpoint_failure_code(status) == "GMAIL_AUTH_REVOKED"


@pytest.mark.parametrize("status", [500, 502, 503, 429, None])
def test_transient_failures_map_to_connection_error(status) -> None:
    """5xx、限流与传输层失败是暂时性的。"""
    assert token_endpoint_failure_code(status) == "GMAIL_CONNECTION_ERROR"


# --------------------------------------------------------------------------
# 令牌到期
# --------------------------------------------------------------------------


def test_expiry_adds_the_reported_lifetime() -> None:
    """到期时刻 = 当前时间 + Google 上报的有效秒数。"""
    assert calculate_access_token_expiry(NOW, 3600) == "2026-10-12T10:00:00.000Z"


def test_expiry_handles_small_and_zero_lifetimes() -> None:
    """零秒或极短有效期照实计算，不特殊处理。"""
    assert calculate_access_token_expiry(NOW, 0) == "2026-10-12T09:00:00.000Z"
    assert calculate_access_token_expiry(NOW, 1) == "2026-10-12T09:00:01.000Z"


def test_absurd_lifetime_yields_none() -> None:
    """**溢出时返回 None**，让调用方判定响应畸形，而不是产出一个离谱的到期时间。"""
    assert calculate_access_token_expiry(NOW, 10**18) is None


# --------------------------------------------------------------------------
# 凭据解密的两类失败
# --------------------------------------------------------------------------


class _Connection:
    """最小连接桩。"""

    def __init__(self, *, status: str = "active", token=None, id: str = "c1") -> None:
        self.status = status
        self.encrypted_refresh_token = token
        self.id = id


class _ExplodingCipher:
    """解密总是失败的加密器。"""

    def decrypt(self, envelope, context):
        raise ValueError("bad tag")


def test_disconnected_connection_requires_reauthorization() -> None:
    """断开或没有密文 → 用户重新授权即可。"""
    with pytest.raises(GmailSyncError) as raised:
        decrypt_connection_refresh_token(_Connection(token=None), _ExplodingCipher())

    assert raised.value.code == "GMAIL_AUTH_REVOKED"


def test_decryption_failure_is_a_connection_error_not_a_revocation() -> None:
    """**解密失败不是"授权被撤销"。**

    问题在**服务端密钥**，让用户反复重新授权是徒劳的。两者混为一谈会让真正的密钥
    问题被掩盖在一连串"请重新登录"的提示后面。
    """
    with pytest.raises(GmailSyncError) as raised:
        decrypt_connection_refresh_token(
            _Connection(token="v1.iv.ct.tag"), _ExplodingCipher()
        )

    assert raised.value.code == "GMAIL_CONNECTION_ERROR"
    assert "密钥" in raised.value.message


def test_successful_decryption_uses_the_connection_id_as_aad() -> None:
    """解密用连接 ID 作为 AAD（与加密时一致）。"""
    captured: dict = {}

    class _Cipher:
        def decrypt(self, envelope, context):
            captured["context"] = context
            return "1//refresh"

    token = decrypt_connection_refresh_token(
        _Connection(token="v1.iv.ct.tag", id="c99"), _Cipher()
    )

    assert token == "1//refresh"
    assert captured["context"] == "gmail-connection:c99"


# --------------------------------------------------------------------------
# 表单构造
# --------------------------------------------------------------------------


def test_exchange_form_contains_the_required_fields() -> None:
    """交换表单含 code / verifier / client_id / redirect_uri / grant_type。"""
    form = build_token_exchange_form(
        authorization_code="code-1", code_verifier="v" * 43,
        client_id="client-1", redirect_uri=REDIRECT,
    )

    assert form == {
        "code": "code-1", "code_verifier": "v" * 43, "client_id": "client-1",
        "redirect_uri": REDIRECT, "grant_type": "authorization_code",
    }


def test_exchange_form_includes_an_optional_secret() -> None:
    """给了密钥就带上。"""
    form = build_token_exchange_form(
        authorization_code="c", code_verifier="v", client_id="i",
        redirect_uri=REDIRECT, client_secret="s",
    )

    assert form["client_secret"] == "s"


def test_refresh_form_omits_redirect_uri() -> None:
    """**刷新不需要 ``redirect_uri``**：它只在授权码交换时用于校验来源。"""
    form = build_token_refresh_form(refresh_token="r", client_id="i")

    assert form == {
        "refresh_token": "r", "client_id": "i", "grant_type": "refresh_token",
    }
    assert "redirect_uri" not in form


@pytest.mark.parametrize(
    "kwargs",
    [
        {"authorization_code": "", "code_verifier": "v", "client_id": "i", "redirect_uri": REDIRECT},
        {"authorization_code": "c", "code_verifier": "  ", "client_id": "i", "redirect_uri": REDIRECT},
        {"authorization_code": "c", "code_verifier": "v", "client_id": "", "redirect_uri": REDIRECT},
        {"authorization_code": "c", "code_verifier": "v", "client_id": "i", "redirect_uri": " "},
    ],
)
def test_exchange_form_rejects_blank_required_fields(kwargs: dict) -> None:
    """必填参数为空必须拒绝，避免向 Google 发送不完整授权请求。"""
    with pytest.raises(ValueError, match="不能为空"):
        build_token_exchange_form(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [{"refresh_token": "", "client_id": "i"}, {"refresh_token": "r", "client_id": "  "}],
)
def test_refresh_form_rejects_blank_fields(kwargs: dict) -> None:
    """刷新表单的必填参数同样拒绝空值。"""
    with pytest.raises(ValueError, match="不能为空"):
        build_token_refresh_form(**kwargs)


# --------------------------------------------------------------------------
# 令牌响应解析
# --------------------------------------------------------------------------


def _token_payload(**overrides) -> dict:
    base = {
        "token_type": "Bearer", "expires_in": 3599,
        "access_token": "ya29.access", "refresh_token": "1//refresh", "scope": "openid email",
    }
    base.update(overrides)
    return base


def test_parses_a_valid_token_response() -> None:
    """合法响应解析出四个字段。"""
    parsed = parse_google_token_response(_token_payload())

    assert parsed.access_token == "ya29.access"
    assert parsed.refresh_token == "1//refresh"
    assert parsed.granted_scope_text == "openid email"
    assert parsed.expires_in_seconds == 3599


@pytest.mark.parametrize("token_type", ["Bearer", "bearer", "BEARER"])
def test_token_type_is_case_insensitive(token_type: str) -> None:
    """``token_type`` 大小写不敏感。"""
    assert parse_google_token_response(
        _token_payload(token_type=token_type)
    ).access_token == "ya29.access"


@pytest.mark.parametrize("token_type", ["mac", "Basic", "", None, 5])
def test_non_bearer_token_type_is_rejected(token_type: object) -> None:
    """**非 bearer 类型必须拒绝。**

    把它当 Bearer 令牌用，会在下一次调用时报出一个与根因无关的 401——
    排查方向完全错了。
    """
    with pytest.raises(GmailOAuthError) as raised:
        parse_google_token_response(_token_payload(token_type=token_type))

    assert raised.value.code == "GOOGLE_TOKEN_EXCHANGE_FAILED"


@pytest.mark.parametrize("expires_in", [0, -1, 1.5, "3600", True, None])
def test_invalid_expiry_is_rejected(expires_in: object) -> None:
    """**``expires_in`` 必须是正安全整数**：零或负值会让令牌"生下来就过期"。"""
    with pytest.raises(GmailOAuthError):
        parse_google_token_response(_token_payload(expires_in=expires_in))


@pytest.mark.parametrize("field", ["access_token", "refresh_token", "scope"])
def test_missing_required_fields_are_rejected(field: str) -> None:
    """缺字段或字段为空都拒绝。"""
    payload = _token_payload()
    del payload[field]

    with pytest.raises(GmailOAuthError):
        parse_google_token_response(payload)


def test_non_object_payload_is_rejected() -> None:
    """非对象负载拒绝。"""
    with pytest.raises(GmailOAuthError):
        parse_google_token_response(["not", "an", "object"])


# --------------------------------------------------------------------------
# 身份解析
# --------------------------------------------------------------------------


def _identity_payload(**overrides) -> dict:
    base = {"sub": "112233", "email": "Owner@Example.COM", "email_verified": True}
    base.update(overrides)
    return base


def test_parses_and_lowercases_the_verified_identity() -> None:
    """身份解析成功，邮箱小写化。"""
    subject, email = parse_verified_identity(_identity_payload())

    assert subject == "112233"
    assert email == "owner@example.com"


@pytest.mark.parametrize("value", [False, None, "true", 1, "True"])
def test_email_verified_must_be_the_boolean_true(value: object) -> None:
    """**``email_verified`` 必须是布尔 ``true``，不是"真值"。**

    接受 ``"true"`` 或 ``1`` 会让"未验证邮箱"在某些响应形状下被当成已验证——
    而邮箱是账户归属的依据。
    """
    with pytest.raises(GmailOAuthError) as raised:
        parse_verified_identity(_identity_payload(email_verified=value))

    assert raised.value.code == "GOOGLE_IDENTITY_UNVERIFIED"


@pytest.mark.parametrize("email", ["", "  ", "not-an-email", "a@", "@b"])
def test_malformed_emails_are_rejected(email: str) -> None:
    """邮箱格式非法必须拒绝。"""
    with pytest.raises(GmailOAuthError) as raised:
        parse_verified_identity(_identity_payload(email=email))

    assert raised.value.code == "GOOGLE_IDENTITY_UNVERIFIED"


def test_missing_subject_is_rejected() -> None:
    """缺少 subject 必须拒绝。"""
    payload = _identity_payload()
    del payload["sub"]

    with pytest.raises(GmailOAuthError):
        parse_verified_identity(payload)


# --------------------------------------------------------------------------
# 端点错误映射
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_retryable_endpoint_errors(status: int) -> None:
    """**只有 429 与 5xx 可重试。**"""
    error = google_endpoint_error("token", status)

    assert error.code == "GOOGLE_TOKEN_EXCHANGE_FAILED"
    assert error.retryable is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retryable(status: int) -> None:
    """4xx 重试不会改变结果。"""
    assert google_endpoint_error("userinfo", status).retryable is False


def test_endpoint_specific_messages() -> None:
    """两个端点的提示文案不同，便于用户判断该做什么。"""
    token_error = google_endpoint_error("token", 500)
    userinfo_error = google_endpoint_error("userinfo", 500)

    assert token_error.message != userinfo_error.message
    assert "授权" in token_error.message
    assert "身份" in userinfo_error.message


# --------------------------------------------------------------------------
# HTTP 编排
# --------------------------------------------------------------------------


def _post_form_ok(payload: dict):
    """构造一个成功的令牌端点桩。"""
    def post_form(url, form, timeout):
        post_form.calls.append({"url": url, "form": dict(form), "timeout": timeout})
        return payload
    post_form.calls = []
    return post_form


def _userinfo_ok(payload: dict):
    """构造一个成功的 userinfo 桩。"""
    def get_userinfo(url, headers, timeout):
        get_userinfo.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        return payload
    get_userinfo.calls = []
    return get_userinfo


def test_exchange_returns_tokens_identity_and_grant() -> None:
    """交换返回临时令牌、已验证身份与离线 grant。"""
    post_form = _post_form_ok(_token_payload(scope="openid email " + GMAIL_READONLY_SCOPE))
    get_userinfo = _userinfo_ok(_identity_payload())

    result = exchange_google_authorization_code(
        authorization_code="code-1", code_verifier="v" * 43, client_id="client-1",
        redirect_uri=REDIRECT, now=NOW, post_form=post_form, get_userinfo=get_userinfo,
    )

    assert result["accessToken"] == "ya29.access"
    assert result["accessTokenExpiresAt"] == "2026-10-12T09:59:59.000Z"
    assert result["identity"] == {
        "subject": "112233", "email": "owner@example.com", "emailVerified": True,
    }
    assert result["grant"]["refreshToken"] == "1//refresh"
    # 作用域被去重排序后存储。
    assert result["grant"]["grantedScopeText"].split(" ") == sorted(
        ["email", "openid", GMAIL_READONLY_SCOPE]
    )


def test_exchange_sends_the_verifier_to_the_token_endpoint() -> None:
    """PKCE verifier 只发给令牌端点，不出现在 userinfo 请求里。"""
    post_form = _post_form_ok(_token_payload(scope=GMAIL_READONLY_SCOPE))
    get_userinfo = _userinfo_ok(_identity_payload())

    exchange_google_authorization_code(
        authorization_code="code-1", code_verifier="v" * 43, client_id="client-1",
        redirect_uri=REDIRECT, now=NOW, post_form=post_form, get_userinfo=get_userinfo,
    )

    assert post_form.calls[0]["form"]["code_verifier"] == "v" * 43
    assert post_form.calls[0]["url"] == GOOGLE_TOKEN_ENDPOINT


def test_exchange_rejects_a_grant_without_the_readonly_scope() -> None:
    """**授权没给只读权限时交换失败。**

    否则连接会"成功建立"但永远读不到邮件，用户看到的是一个安静失效的连接。
    """
    post_form = _post_form_ok(_token_payload(scope="openid email"))
    get_userinfo = _userinfo_ok(_identity_payload())

    with pytest.raises(GmailOAuthError) as raised:
        exchange_google_authorization_code(
            authorization_code="code-1", code_verifier="v" * 43, client_id="client-1",
            redirect_uri=REDIRECT, now=NOW, post_form=post_form, get_userinfo=get_userinfo,
        )

    assert raised.value.code == "GMAIL_SCOPE_MISSING"


def test_exchange_rejects_an_unverified_email() -> None:
    """身份未验证时交换失败。"""
    post_form = _post_form_ok(_token_payload(scope=GMAIL_READONLY_SCOPE))
    get_userinfo = _userinfo_ok(_identity_payload(email_verified=False))

    with pytest.raises(GmailOAuthError) as raised:
        exchange_google_authorization_code(
            authorization_code="code-1", code_verifier="v" * 43, client_id="client-1",
            redirect_uri=REDIRECT, now=NOW, post_form=post_form, get_userinfo=get_userinfo,
        )

    assert raised.value.code == "GOOGLE_IDENTITY_UNVERIFIED"


def test_exchange_maps_transport_failure_at_the_token_endpoint() -> None:
    """令牌端点的网络中断映射为安全、可重试错误，不伪装成参数无效。"""
    def post_form(url, form, timeout):
        raise GoogleEndpointFailure(status=None, retryable=True)

    with pytest.raises(GmailOAuthError) as raised:
        exchange_google_authorization_code(
            authorization_code="c", code_verifier="v", client_id="i",
            redirect_uri=REDIRECT, now=NOW, post_form=post_form,
            get_userinfo=_userinfo_ok(_identity_payload()),
        )

    assert "无法连接 Google" in raised.value.message
    assert raised.value.retryable is True


def test_userinfo_failure_uses_the_userinfo_message() -> None:
    """userinfo 失败用身份相关的文案，便于用户判断该做什么。"""
    def get_userinfo(url, headers, timeout):
        raise GoogleEndpointFailure(status=500, retryable=True)

    with pytest.raises(GmailOAuthError) as raised:
        exchange_google_authorization_code(
            authorization_code="c", code_verifier="v", client_id="i",
            redirect_uri=REDIRECT, now=NOW,
            post_form=_post_form_ok(_token_payload(scope=GMAIL_READONLY_SCOPE)),
            get_userinfo=get_userinfo,
        )

    assert "身份" in raised.value.message
    assert raised.value.retryable is True


# --------------------------------------------------------------------------
# 刷新编排
# --------------------------------------------------------------------------


def test_refresh_returns_a_new_access_token() -> None:
    """刷新返回新的临时令牌。"""
    post_form = _post_form_ok(_token_payload(
        scope=GMAIL_READONLY_SCOPE, access_token="ya29.second"
    ))

    result = refresh_google_access_token(
        refresh_token="1//refresh", client_id="client-1",
        granted_scope_text=GMAIL_READONLY_SCOPE, now=NOW, post_form=post_form,
    )

    assert result["accessToken"] == "ya29.second"
    assert post_form.calls[0]["form"]["grant_type"] == "refresh_token"
    assert "redirect_uri" not in post_form.calls[0]["form"]


def test_refresh_allows_google_to_omit_refresh_token_and_unchanged_scope() -> None:
    """refresh response can omit the already stored refresh token and unchanged scope."""
    post_form = _post_form_ok({
        "access_token": "ya29.second",
        "token_type": "Bearer",
        "expires_in": 3600,
    })

    result = refresh_google_access_token(
        refresh_token="1//refresh", client_id="client-1",
        granted_scope_text=GMAIL_READONLY_SCOPE, now=NOW, post_form=post_form,
    )

    assert result["accessToken"] == "ya29.second"
    assert result["grantedScopeText"] == GMAIL_READONLY_SCOPE


def test_refresh_revalidates_scope_when_google_returns_it() -> None:
    """refresh response scope is validated when present rather than blindly trusted."""
    post_form = _post_form_ok({
        "access_token": "ya29.second",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid email",
    })

    with pytest.raises(GmailOAuthError) as raised:
        refresh_google_access_token(
            refresh_token="1//refresh", client_id="client-1",
            granted_scope_text=GMAIL_READONLY_SCOPE, now=NOW, post_form=post_form,
        )

    assert raised.value.code == "GMAIL_SCOPE_MISSING"


def test_refresh_maps_revoked_credentials_to_auth_revoked() -> None:
    """**400 表示 refresh token 已被撤销** → 需要用户重新授权。"""
    def post_form(url, form, timeout):
        raise GoogleEndpointFailure(status=400, retryable=False, oauth_error_code="invalid_grant")

    with pytest.raises(GmailSyncError) as raised:
        refresh_google_access_token(
            refresh_token="r", client_id="i", granted_scope_text=GMAIL_READONLY_SCOPE,
            now=NOW, post_form=post_form,
        )

    assert raised.value.code == "GMAIL_AUTH_REVOKED"


def test_refresh_maps_server_errors_to_connection_error() -> None:
    """5xx 是暂时性故障。"""
    def post_form(url, form, timeout):
        raise GoogleEndpointFailure(status=503, retryable=True)

    with pytest.raises(GmailSyncError) as raised:
        refresh_google_access_token(
            refresh_token="r", client_id="i", granted_scope_text=GMAIL_READONLY_SCOPE,
            now=NOW, post_form=post_form,
        )

    assert raised.value.code == "GMAIL_CONNECTION_ERROR"
    assert raised.value.retryable is True


def test_refresh_rejects_connections_without_the_readonly_scope() -> None:
    """存储的作用域不对时，连刷新都不必做。"""
    with pytest.raises(GmailOAuthError) as raised:
        refresh_google_access_token(
            refresh_token="r", client_id="i", granted_scope_text="openid",
            now=NOW, post_form=_post_form_ok(_token_payload()),
        )

    assert raised.value.code == "GMAIL_SCOPE_MISSING"


# --------------------------------------------------------------------------
# 撤销
# --------------------------------------------------------------------------


class _PlainCipher:
    """解密总是返回固定值的加密器。"""

    def __init__(self, value: str = "1//refresh") -> None:
        self._value = value

    def decrypt(self, envelope, context):
        return self._value


def test_revocation_sends_the_refresh_token() -> None:
    """撤销把 refresh token 发给撤销端点。"""
    calls: list = []

    def post_form(url, form, timeout):
        calls.append({"url": url, "form": dict(form)})

    revoke_gmail_connection_authorization(
        _Connection(token="v1.x.y.z"), _PlainCipher(), post_form=post_form
    )

    assert calls[0]["url"] == GOOGLE_TOKEN_REVOCATION_ENDPOINT
    assert calls[0]["form"] == {"token": "1//refresh"}


def test_revocation_failure_is_reported_not_swallowed() -> None:
    """**撤销失败必须上报。**

    静默成功会让用户以为凭据已作废，而 Google 侧仍然有效。
    """
    def post_form(url, form, timeout):
        raise GoogleEndpointFailure(status=500, retryable=True)

    with pytest.raises(GmailSyncError) as raised:
        revoke_gmail_connection_authorization(
            _Connection(token="v1.x.y.z"), _PlainCipher(), post_form=post_form
        )

    assert raised.value.code == "GMAIL_CONNECTION_ERROR"
    assert raised.value.retryable is True


def test_revocation_does_not_call_google_for_a_disconnected_connection() -> None:
    """已断开的连接没有凭据可撤销，不发请求。"""
    calls: list = []

    def post_form(url, form, timeout):
        calls.append(url)

    with pytest.raises(GmailSyncError) as raised:
        revoke_gmail_connection_authorization(
            _Connection(status="disconnected", token=None), _PlainCipher(),
            post_form=post_form,
        )

    assert raised.value.code == "GMAIL_AUTH_REVOKED"
    assert calls == []
