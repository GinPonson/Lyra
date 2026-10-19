"""认证上下文与用户范围依赖的测试。

覆盖 P0 契约 §3.8 的全部约定：

1. ``AuthContext`` 不可变，且**只能由认证器在「令牌 → 设备 → 用户」全部通过后产出**；
2. ``require_user`` 把它写入 ``request.state.auth``，并**不再**写入 ``request.state.device``
   ——两条来源会让归属判定分叉；
3. 用户已停用返回 ``USER_DISABLED``（403，``retryable=false``），而不是 401：
   客户端据此结束会话，而不是进入永远失败的刷新重试；
4. ``require_admin`` 在 ``require_user`` 之上判定角色，非管理员 403 ``ADMIN_REQUIRED``。

这些用例直接调用依赖函数并传入构造出来的 ``Request``：HTTP 层的行为由
``test_auth_routes.py`` 覆盖，这里要固定的是依赖自身的契约。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from lyra.api.dependencies import require_admin, require_user
from lyra.application.device_authenticator import DeviceAuthenticator
from lyra.application.device_pairing_service import DevicePairingService
from lyra.application.write_coordinator import WriteCoordinator
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.infrastructure.db.user_repository import UserRepository
from lyra.security.auth_context import AuthContext

PUBLIC_KEY_PEM = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"][0]["inputPem"]
IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")


@pytest.fixture()
def wired(tmp_path: Path):
    """真实库 + 真实设备签发：产出一个可直接用于认证的令牌。"""
    database = open_lyra_database(str(tmp_path / "auth-context.duckdb"))
    connection = database.connection
    service = DevicePairingService(
        DuckDbDeviceRepository(connection), WriteCoordinator(), UserRepository(connection)
    )
    issued = asyncio.run(service.authorize_user_device(
        identity=IDENTITY, display_name="Pixel 9", public_key_pem=PUBLIC_KEY_PEM
    ))
    app = FastAPI()
    app.state.authenticator = DeviceAuthenticator(
        DuckDbDeviceRepository(connection), lambda: datetime.now(UTC)
    )
    try:
        yield app, connection, issued
    finally:
        database.close()


def _request(app: FastAPI, token: str | None = None) -> Request:
    """构造一个只带授权头的请求对象。

    ``app.state.authenticator`` 由依赖从 ``request.app`` 取出，因此这里必须挂真实应用。
    """
    headers = [] if token is None else [(b"authorization", f"Bearer {token}".encode())]
    return Request({"type": "http", "app": app, "headers": headers, "method": "GET", "path": "/"})


def test_auth_context_is_frozen(wired) -> None:
    """上下文一旦产出就不可修改：改它等于在请求中途伪造归属。"""
    context = AuthContext(user_id="u", device_id="d", role="member")

    with pytest.raises(FrozenInstanceError):
        context.user_id = "other"  # type: ignore[misc]


def test_require_user_returns_the_context_and_writes_request_state(wired) -> None:
    """依赖返回上下文，并把它挂到 ``request.state.auth`` 供处理器读取。"""
    app, _, issued = wired
    request = _request(app, issued.access_token)

    context = require_user(request, f"Bearer {issued.access_token}")

    assert isinstance(context, AuthContext)
    assert context.user_id == issued.user.id
    assert context.device_id == issued.device_id
    assert context.role == "member"
    assert request.state.auth is context


def test_require_user_no_longer_exposes_request_state_device(wired) -> None:
    """``request.state.device`` 必须消失。

    保留它会让「当前用户」有两条来源：处理器读 ``device`` 时拿到的是设备（没有
    user_id），读 ``auth`` 时拿到的是用户，两者的归属判定会分叉。
    """
    app, _, issued = wired
    request = _request(app, issued.access_token)

    require_user(request, f"Bearer {issued.access_token}")

    assert not hasattr(request.state, "device")


@pytest.mark.parametrize("authorization", [None, "", "Bearer", "Bearer ", "token", "Basic abc"])
def test_require_user_rejects_a_missing_or_malformed_header(wired, authorization) -> None:
    """缺少或格式不符的授权头一律 401，且码固定（不区分原因以免枚举令牌）。"""
    app, _, _ = wired

    with pytest.raises(HTTPException) as raised:
        require_user(_request(app), authorization)

    assert raised.value.status_code == 401
    assert raised.value.detail["code"] == "DEVICE_CREDENTIAL_INVALID"


def test_require_user_rejects_an_unknown_token(wired) -> None:
    """未知令牌 401 ``DEVICE_CREDENTIAL_INVALID``。"""
    app, _, _ = wired

    with pytest.raises(HTTPException) as raised:
        require_user(_request(app), "Bearer nope")

    assert raised.value.status_code == 401
    assert raised.value.detail["code"] == "DEVICE_CREDENTIAL_INVALID"


def test_require_user_rejects_a_revoked_device_with_its_domain_code(wired) -> None:
    """设备撤销保留领域错误码：客户端据此决定重登还是刷新。

    压成 ``DEVICE_CREDENTIAL_INVALID`` 会让 App 无法区分「该重新登录」与「刷新令牌」。
    """
    app, connection, issued = wired
    connection.execute(
        """
        UPDATE paired_devices
        SET status = 'revoked', revoked_at = CAST(? AS TIMESTAMPTZ)
        WHERE id = CAST(? AS UUID)
        """,
        ["2026-10-13T12:00:00.000Z", issued.device_id],
    )

    with pytest.raises(HTTPException) as raised:
        require_user(_request(app), f"Bearer {issued.access_token}")

    assert raised.value.status_code == 401
    assert raised.value.detail["code"] == "DEVICE_REVOKED"


def test_require_user_rejects_a_disabled_user(wired) -> None:
    """**用户停用必须在认证层拒绝**：403 ``USER_DISABLED`` 且不可重试。

    放在认证层而不是各业务仓储，意味着只有一条判定路径——任何一处漏加
    ``status = 'active'`` 条件都会让被停用的用户继续读写账本，而这类遗漏在正常
    路径上不会暴露。
    """
    app, connection, issued = wired
    connection.execute(
        "UPDATE users SET status = 'disabled' WHERE id = CAST(? AS UUID)", [issued.user.id]
    )

    with pytest.raises(HTTPException) as raised:
        require_user(_request(app), f"Bearer {issued.access_token}")

    assert raised.value.status_code == 403
    assert raised.value.detail["code"] == "USER_DISABLED"
    assert raised.value.detail["message"] == "该用户已停用"


def test_require_admin_rejects_a_member(wired) -> None:
    """普通成员访问管理依赖：403 ``ADMIN_REQUIRED``。"""
    app, _, issued = wired
    request = _request(app, issued.access_token)

    with pytest.raises(HTTPException) as raised:
        require_admin(request, f"Bearer {issued.access_token}")

    assert raised.value.status_code == 403
    assert raised.value.detail["code"] == "ADMIN_REQUIRED"


def test_require_admin_reports_a_disabled_user_before_the_role_check(wired) -> None:
    """停用用户的 403 先于角色判定。

    否则一个被停用的管理员会在管理接口上得到「权限不足」，掩盖真正的原因。
    """
    app, connection, issued = wired
    connection.execute(
        "UPDATE users SET role = 'admin', status = 'disabled' WHERE id = CAST(? AS UUID)",
        [issued.user.id],
    )

    with pytest.raises(HTTPException) as raised:
        require_admin(_request(app), f"Bearer {issued.access_token}")

    assert raised.value.status_code == 403
    assert raised.value.detail["code"] == "USER_DISABLED"


def test_require_admin_accepts_an_admin_and_writes_request_state(wired) -> None:
    """管理员通过，角色来自用户行（而不是令牌里的某个声明）。"""
    app, connection, issued = wired
    connection.execute(
        "UPDATE users SET role = 'admin' WHERE id = CAST(? AS UUID)", [issued.user.id]
    )
    request = _request(app, issued.access_token)

    context = require_admin(request, f"Bearer {issued.access_token}")

    assert context.role == "admin"
    assert request.state.auth is context


def test_the_role_is_read_from_the_database_on_every_request(wired) -> None:
    """角色与状态每次都从数据库读：降权与停用必须立刻生效，不等令牌过期。"""
    app, connection, issued = wired
    token = f"Bearer {issued.access_token}"
    assert require_user(_request(app, issued.access_token), token).role == "member"

    connection.execute(
        "UPDATE users SET role = 'admin' WHERE id = CAST(? AS UUID)", [issued.user.id]
    )

    assert require_user(_request(app, issued.access_token), token).role == "admin"
