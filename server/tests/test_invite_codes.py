"""邀请码机制：仓储语义、注册门禁与管理接口。

**这个文件守护的是一条业务边界**：新身份不能自行注册，必须有管理员（或离线入口）
签发的邀请码。因此除了仓储本身的单次有效，还要证明三件事：

1. 注册路径**真的**被拦住（没有码 → 403，码已用/已撤销/不存在 → 403）；
2. 已有用户登录**不受影响**，且不会白烧一枚码；
3. 管理接口只对管理员开放，且明文只在创建响应里出现一次。

注册门禁的实现位置是 ``main._authorize_user_device``（登录兑换唯一入口），所以这里
直接复用生产装配，而不是在测试里拼一份等价实现——"测试复制了生产逻辑"会让门禁
漏装配时无人报警。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.application.device_pairing_service import DevicePairingService
from lyra.application.write_coordinator import WriteCoordinator
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import ConfiguredLoginProvider, LoginIdentity
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.application.login_service import pkce_challenge
from lyra.infrastructure.db.invite_code_repository import (
    InviteCodeRepository,
    hash_invite_code,
)
from lyra.infrastructure.db.user_repository import UserRepository
from lyra.main import _authorize_user_device, create_app
from lyra.protocol import CLIENT_PROTOCOL_HEADERS
from lyra.security.device_auth import create_opaque_credential, hash_opaque_credential

PUBLIC_KEY_PEM = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"][0]["inputPem"]
VERIFIER = "B" * 43
# PKCE 挑战必须是 verifier 的 S256 摘要；写死两个不相干的常量会让兑换停在 401。
CHALLENGE = pkce_challenge(VERIFIER)
IDENTITY = LoginIdentity(provider="github", subject="7001", email=None, display_name="member")


class _ScriptedLoginClient:
    """来源客户端替身：固定授权地址 + 固定身份。"""

    def __init__(self, identity: LoginIdentity) -> None:
        self.identity = identity

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        return f"https://provider.example/authorize?state={state}"

    def exchange_code(self, **kwargs: object) -> LoginIdentity:
        return self.identity


def _settings(tmp_path: Path) -> Settings:
    """构造满足必填项的环境（不配置任何真实 OAuth 凭据）。"""
    return Settings(
        LYRA_DATABASE_PATH=str(tmp_path / "invites.duckdb"),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )


@pytest.fixture()
def app_client(tmp_path: Path):
    """真实应用；登录来源换成脚本客户端，其余全部是生产装配。"""
    database = open_lyra_database(str(tmp_path / "invites.duckdb"))
    application = create_app(_settings(tmp_path), database)
    # 装一个 github 来源，并让它走生产的那条签发链路（含邀请码门禁）。
    from lyra.application.login_service import UserLoginService

    application.state.login_service = UserLoginService(
        [ConfiguredLoginProvider(id="github", name="GitHub", client=_ScriptedLoginClient(IDENTITY))],
        lambda **kwargs: _authorize_user_device(
            database=database,
            write_coordinator=application.state.write_coordinator,
            **kwargs,
        ),
        InviteCodeRepository(database.connection),
    )
    try:
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
            yield client, database
    finally:
        database.close()


def _mint(connection, *, label: str | None = None) -> tuple[str, str]:
    """铸造一枚邀请码，返回 (明文, id)。"""
    record, code = InviteCodeRepository(connection).create(
        created_by=None, label=label, now=datetime.now(UTC)
    )
    return code, record.id


def _start(client: TestClient, *, invite: str | None = None) -> dict:
    """发起登录。"""
    body: dict[str, object] = {
        "displayName": "Pixel 9",
        "publicKeyPem": PUBLIC_KEY_PEM,
        "codeChallenge": CHALLENGE,
        "codeChallengeMethod": "S256",
    }
    if invite is not None:
        body["inviteCode"] = invite
    response = client.post("/v1/auth/github/start", json=body)
    return {"status": response.status_code, "body": response.json()}


def _callback(client: TestClient, started: dict) -> None:
    """走完浏览器回调。"""
    from urllib.parse import parse_qs, urlsplit

    state = parse_qs(urlsplit(started["authorizationUrl"]).query)["state"][0]
    client.get("/v1/auth/github/callback", params={"state": state, "code": "auth-code"})


def _exchange(client: TestClient, started: dict):
    """用 App verifier 兑换。"""
    return client.post(
        "/v1/auth/github/exchange",
        json={"loginId": started["loginId"], "codeVerifier": VERIFIER},
    )


def _login(client: TestClient, *, invite: str | None = None):
    """完整走一次登录，返回 exchange 响应。"""
    start = _start(client, invite=invite)
    assert start["status"] == 200, start
    _callback(client, start["body"])
    return _exchange(client, start["body"])


# ----------------------------------------------------------------------
# 门禁：新身份
# ----------------------------------------------------------------------


def test_a_new_identity_without_an_invite_cannot_register(app_client) -> None:
    """**没有邀请码 → 403 INVITE_CODE_REQUIRED**，而不是悄悄注册。"""
    client, database = app_client

    response = _login(client)

    assert response.status_code == 403
    assert response.json()["code"] == "INVITE_CODE_REQUIRED"
    assert UserRepository(database.connection).find_identity("github", "7001") is None


def test_a_new_identity_with_a_valid_invite_registers_and_consumes_it(app_client) -> None:
    """带码注册成功，且码在同一次事务里被核销给新用户。"""
    client, database = app_client
    code, invite_id = _mint(database.connection, label="给 Alice")

    response = _login(client, invite=code)

    assert response.status_code == 200, response.text
    user_id = response.json()["user"]["userId"]
    record = InviteCodeRepository(database.connection).find_by_id(invite_id)
    assert record is not None
    assert record.status == "used"
    assert record.used_by == user_id
    assert record.used_at is not None
    # 明文与哈希都不应能再从库里读出来：列表接口只给运维视图。
    assert InviteCodeRepository(database.connection).find_by_code(code) is not None


@pytest.mark.parametrize("state", ["used", "revoked"])
def test_an_unusable_invite_is_rejected_with_one_code(app_client, state: str) -> None:
    """已用与已撤销返回**同一个** 403 码：区分它们会泄漏码的状态。"""
    client, database = app_client
    code, invite_id = _mint(database.connection)
    invites = InviteCodeRepository(database.connection)
    if state == "revoked":
        assert invites.revoke(invite_id, now=datetime.now(UTC)) is not None
    else:
        # 造一个"已用"状态：哈希与该码一致，占用者是一个真实用户。
        user = UserRepository(database.connection).find_or_create_user_for_identity(
            IDENTITY, now=datetime.now(UTC)
        )
        assert invites.consume(
            code_hash=hash_invite_code(code), used_by=user.user.id, now=datetime.now(UTC)
        )

    # **在 /start 就被拒**：不必跳一次浏览器才发现码不能用。
    started = _start(client, invite=code)

    assert started["status"] == 403
    assert started["body"]["code"] == "INVITE_CODE_INVALID"


def test_an_unknown_invite_is_rejected(app_client) -> None:
    """不存在的码同样 403 INVITE_CODE_INVALID，且不会创建用户。"""
    client, database = app_client
    started = _start(client, invite="z" * 32)
    assert started["status"] == 403
    assert started["body"]["code"] == "INVITE_CODE_INVALID"
    assert UserRepository(database.connection).find_identity("github", "7001") is None


def test_start_rejects_an_unusable_invite_before_the_oauth_round_trip(app_client) -> None:
    """**在 /start 就失败**：用户不必跳一次浏览器才发现码不对。"""
    client, _ = app_client
    started = _start(client, invite="z" * 32)
    assert started["status"] == 403
    assert started["body"]["code"] == "INVITE_CODE_INVALID"


def test_a_malformed_invite_code_is_a_400_not_a_403(app_client) -> None:
    """格式非法是请求错误（400），"码不可用"是业务状态（403）。"""
    client, _ = app_client
    started = _start(client, invite="短")
    assert started["status"] == 400
    assert started["body"]["code"] == "INVALID_ARGUMENT"


# ----------------------------------------------------------------------
# 门禁：已有身份
# ----------------------------------------------------------------------


def test_an_existing_identity_logs_in_without_an_invite_and_burns_none(app_client) -> None:
    """老用户不带码也能登录，且**不会消耗**任何码。"""
    client, database = app_client
    first_code, _ = _mint(database.connection)
    assert _login(client, invite=first_code).status_code == 200

    spare_code, spare_id = _mint(database.connection)
    second = _login(client)  # 不带码

    assert second.status_code == 200
    spare = InviteCodeRepository(database.connection).find_by_id(spare_id)
    assert spare is not None and spare.status == "active", "老用户登录不应消耗邀请码"


def test_an_existing_identity_may_also_carry_a_code_and_it_stays_active(app_client) -> None:
    """即便客户端每次都把码带上，已有身份也不会把它用掉。"""
    client, database = app_client
    first, _ = _mint(database.connection)
    assert _login(client, invite=first).status_code == 200

    extra, extra_id = _mint(database.connection)
    assert _login(client, invite=extra).status_code == 200
    record = InviteCodeRepository(database.connection).find_by_id(extra_id)
    assert record is not None and record.status == "active"


# ----------------------------------------------------------------------
# 仓储语义
# ----------------------------------------------------------------------


def test_the_repository_stores_only_the_hash_and_consumes_once(app_client) -> None:
    """库里没有明文；同一枚码第二次核销必然失败（CAS）。"""
    _, database = app_client
    invites = InviteCodeRepository(database.connection)
    code, invite_id = _mint(database.connection)
    digest = hash_invite_code(code)

    stored = database.connection.execute(
        "SELECT code_hash FROM invite_codes WHERE id = CAST(? AS UUID)", [invite_id]
    ).fetchone()
    assert stored == (digest,)
    assert code not in json.dumps(stored)

    user = UserRepository(database.connection).find_or_create_user_for_identity(
        IDENTITY, now=datetime.now(UTC)
    )
    assert invites.consume(code_hash=digest, used_by=user.user.id, now=datetime.now(UTC)) is True
    assert invites.consume(code_hash=digest, used_by=user.user.id, now=datetime.now(UTC)) is False


def test_revoking_is_idempotent_and_only_for_active_codes(app_client) -> None:
    """撤销只对活动码生效；重复撤销返回 None（由接口翻成 409）。"""
    _, database = app_client
    invites = InviteCodeRepository(database.connection)
    _, invite_id = _mint(database.connection)

    first = invites.revoke(invite_id, now=datetime.now(UTC))
    assert first is not None and first.status == "revoked" and first.revoked_at is not None
    assert invites.revoke(invite_id, now=datetime.now(UTC)) is None


# ----------------------------------------------------------------------
# 管理接口
# ----------------------------------------------------------------------


def _admin_headers(connection) -> dict[str, str]:
    """造一个管理员设备，返回认证头。"""
    repository = DuckDbDeviceRepository(connection)
    pairing = DevicePairingService(
        repository,
        WriteCoordinator(),
        UserRepository(connection),
        now=lambda: datetime.now(UTC),
    )

    async def issue() -> tuple[str, str]:
        users = UserRepository(connection)
        admin = users.create_admin(
            LoginIdentity("google", "1", None, "Admin"), now=datetime.now(UTC)
        )
        issued = await pairing.authorize_user_device(
            identity=LoginIdentity("google", "1", None, "Admin"),
            display_name="Admin Phone",
            public_key_pem=PUBLIC_KEY_PEM,
        )
        return issued.access_token, admin.user.id

    token, user_id = asyncio.run(issue())
    return {"Authorization": f"Bearer {token}", "x-lyra-protocol-version": "3"}, user_id


def test_admin_creates_lists_and_revokes_invite_codes(app_client) -> None:
    """创建返回明文一次；列表不含明文；撤销后不可用。"""
    client, database = app_client
    headers, admin_id = _admin_headers(database.connection)

    created = client.post("/v1/admin/invite-codes", json={"label": "给 Bob"}, headers=headers)
    assert created.status_code == 201, created.text
    body = created.json()["inviteCode"]
    code, invite_id = body["code"], body["id"]
    assert body["status"] == "active"
    assert body["label"] == "给 Bob"
    assert body["createdBy"] == admin_id

    listed = client.get("/v1/admin/invite-codes", headers=headers).json()
    assert listed["count"] == 1
    assert "code" not in listed["inviteCodes"][0], "列表不得返回明文"

    revoked = client.delete(f"/v1/admin/invite-codes/{invite_id}", headers=headers)
    assert revoked.status_code == 200
    assert revoked.json()["inviteCode"]["status"] == "revoked"
    # 再撤一次是 409，不是静默成功
    assert client.delete(f"/v1/admin/invite-codes/{invite_id}", headers=headers).status_code == 409

    # 撤销后的码无法注册（在 /start 就被拒）
    rejected = _start(client, invite=code)
    assert rejected["status"] == 403
    assert rejected["body"]["code"] == "INVITE_CODE_INVALID"


def test_the_created_code_actually_registers_a_user(app_client) -> None:
    """管理接口签发的码必须真的能用**同一台服务器**注册。"""
    client, database = app_client
    headers, _ = _admin_headers(database.connection)
    code = client.post("/v1/admin/invite-codes", json={}, headers=headers).json()["inviteCode"]["code"]

    response = _login(client, invite=code)

    assert response.status_code == 200, response.text
    assert response.json()["user"]["role"] == "member"


def test_invite_management_requires_an_admin(app_client) -> None:
    """普通成员与未认证请求都不能签发或列举。"""
    client, database = app_client
    member_code, _ = _mint(database.connection)
    assert _login(client, invite=member_code).status_code == 200

    # 造一台成员设备（与生产同构：只加一行访问令牌，密钥材料不参与本用例）
    token = create_opaque_credential()
    now = datetime.now(UTC).isoformat()
    database.connection.execute(
        "INSERT INTO device_access_tokens (id, device_id, token_hash, issued_at, expires_at, revoked_at) "
        "SELECT CAST(? AS UUID), d.id, ?, CAST(? AS TIMESTAMP), CAST(? AS TIMESTAMP), NULL "
        "FROM paired_devices d LIMIT 1",
        [str(__import__("uuid").uuid4()), hash_opaque_credential(token.token), now, "2099-01-01T00:00:00"],
    )
    member_headers = {
        "Authorization": f"Bearer {token.token}",
        "x-lyra-protocol-version": "3",
    }

    assert client.post("/v1/admin/invite-codes", json={}, headers=member_headers).status_code == 403
    assert client.get("/v1/admin/invite-codes", headers=member_headers).status_code == 403
    assert client.post("/v1/admin/invite-codes", json={}).status_code == 401


def test_providers_advertise_that_registration_needs_an_invite(app_client) -> None:
    """登录页要据此显示邀请码输入框，因此这个能力位必须是公开且显式的。"""
    client, _ = app_client
    body = client.get("/v1/auth/providers").json()
    assert body["registrationRequiresInvite"] is True
