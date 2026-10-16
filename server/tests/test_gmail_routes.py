"""Gmail 路由测试。

三处**在路由层**完成的格式校验早于任何业务逻辑：``state`` 形状、UUID 形状、
``YYYY-MM-DD``。放在这一层的价值是**错误信息能指明是哪一段**，而不是让下游抛一个
笼统的"参数无效"。

多用户下还固定两条：

- 每条业务动作都在**用户绑定视图**（``for_user(user_id)``）上执行，用户取自
  ``request.state.auth.user_id``；
- 用户停用后，Gmail 的入队与执行都被拒绝（403 ``USER_DISABLED``）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.protocol import CLIENT_PROTOCOL_HEADERS
from lyra.application.app_error import ApplicationError
from lyra.application.device_pairing_service import DevicePairingService
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.infrastructure.db.user_repository import UserRepository
from lyra.main import create_app

PUBLIC_KEY_PEM = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"][0]["inputPem"]
IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")
CONNECTION_ID = "11111111-1111-1111-1111-111111111111"
MAPPING_ID = "22222222-2222-2222-2222-222222222222"
ACCOUNT_ID = "33333333-3333-3333-3333-333333333333"
STATE = "A" * 43

GMAIL_ROUTE_PATHS = [
    ("post", "/v1/google/authorization-transactions"),
    ("post", f"/v1/google/authorization-transactions/{STATE}/complete"),
    ("get", "/v1/google/connections"),
    ("get", f"/v1/google/connections/{CONNECTION_ID}/mappings"),
    ("put", f"/v1/google/connections/{CONNECTION_ID}/mappings/{MAPPING_ID}"),
    ("post", f"/v1/google/connections/{CONNECTION_ID}/sync"),
    ("delete", f"/v1/google/connections/{CONNECTION_ID}"),
]
class _FakeGmailUserView:
    """用户绑定视图桩：业务调用记录在父桩上，``user_id`` 由父桩统一记账。"""

    def __init__(self, parent: FakeGmailService) -> None:
        self._parent = parent

    def __getattr__(self, name: str):
        return self._parent._action(name)


class FakeGmailService:
    """记录调用的服务桩。

    **记录与返回值并存**：覆写函数只决定返回什么（或抛什么错），调用仍会被记下来。
    早期版本在覆写时直接返回覆写函数，于是断言 `calls` 的测试全部落空。

    **``for_user`` 记录用户而不是业务调用**：``calls`` 的形状（含既有断言）只描述
    业务动作；用户范围单独记在 ``scoped_user_ids``，这样"路由是否用当前用户取视图"
    可以被独立断言。桩上**没有**业务方法本身——路由若忘了 ``for_user`` 会直接
    ``AttributeError``，而不是静默地按无用户范围执行。
    """

    def __init__(self, **overrides) -> None:
        self.calls: list[tuple] = []
        self.scoped_user_ids: list[str] = []
        self._overrides = overrides

    def for_user(self, user_id: str) -> _FakeGmailUserView:
        """返回用户绑定视图。"""
        self.scoped_user_ids.append(user_id)
        return _FakeGmailUserView(self)

    def _action(self, name: str):
        def action(*args, **kwargs):
            # 参考实现里 `createAuthorizationTransaction(device.id)` 是**位置参数**
            # 调用，其余动作是关键字调用；桩两者都要接。
            self.calls.append((name, kwargs, args))
            override = self._overrides.get(name)
            if override is None:
                return {"ok": name}
            return override(*args, **kwargs)

        return action


@pytest.fixture()
def wired(tmp_path: Path):
    """装配应用与一个有效令牌。"""
    database_path = tmp_path / "gmail-routes.duckdb"
    database = open_lyra_database(str(database_path))
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    application = create_app(settings, database)
    service = DevicePairingService(
        DuckDbDeviceRepository(database.connection),
        application.state.write_coordinator,
        UserRepository(database.connection),
    )
    issued = asyncio.run(service.authorize_user_device(
        identity=IDENTITY, display_name="Pixel 9", public_key_pem=PUBLIC_KEY_PEM
    ))
    try:
        yield application, issued
    finally:
        database.close()


def _auth(issued) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {issued.access_token}",
        # **每个 /v1 请求都必须声明客户端协议版本**（除公开路由）。
        # 缺失时服务端返回 426——那是客户端唯一能据此升级 App 的信号。
        # **引用共享常量而不是字面量**：写死会在协议升版后静默变成 426。
        **CLIENT_PROTOCOL_HEADERS,
    }


def _with_service(application, service) -> None:
    """替换应用上的 Gmail 服务（``None`` 表示移除）。"""
    application.state.gmail_service = service


# --------------------------------------------------------------------------
# 认证
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [pytest.param(method, path, id=f"{method} {path}") for method, path in GMAIL_ROUTE_PATHS],
)
def test_every_gmail_route_requires_authentication(wired, method: str, path: str) -> None:
    """**7 条路由全部要求设备认证**——这是私有账本。"""
    application, _ = wired
    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        request = getattr(client, method)
        response = request(path, json={}) if method in ("post", "put") else request(path)

    assert response.status_code == 401, f"{method} {path}"


def test_service_absent_reports_503_not_500(wired) -> None:
    """未装配服务：503 而不是 500——服务是好的，只是少了配置。

    **服务现在由 `main.py` 装配**（§134），因此这条测试显式移除它来覆盖该分支，
    而不是依赖"默认没有服务"。
    """
    application, issued = wired
    _with_service(application, None)

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.get("/v1/google/connections", headers=_auth(issued))

    assert response.status_code == 503
    assert response.json()["code"] == "GMAIL_CONNECTION_ERROR"


def test_the_real_service_is_wired_by_default(wired) -> None:
    """**默认装配的是真实服务**，不是桩。

    这条测试的价值与其它相反：其它测试都替换掉服务，因此"是否真的接线了"没有任何
    测试覆盖——一个被删掉的 `main.py` 装配行不会被任何人发现。
    """
    application, issued = wired

    assert application.state.gmail_service is not None
    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.get("/v1/google/connections", headers=_auth(issued))

    assert response.status_code == 200
    assert response.json() == {"connections": []}


def test_the_real_service_reports_unconfigured_oauth(wired) -> None:
    """真实服务在没有 Google 配置时如实返回 503（而不是崩溃）。

    本夹具未设置 ``LYRA_GOOGLE_CLIENT_ID``，因此这是"未配置"而非"故障"。
    """
    application, issued = wired

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.post(
            "/v1/google/authorization-transactions", headers=_auth(issued)
        )

    assert response.status_code == 503
    assert response.json()["code"] == "GMAIL_CONNECTION_ERROR"


# --------------------------------------------------------------------------
# 授权事务
# --------------------------------------------------------------------------


def test_creates_an_authorization_transaction(wired) -> None:
    """创建事务返回 201 并绑定发起设备。"""
    application, issued = wired
    service = FakeGmailService(
        create_authorization_transaction=lambda *args, **kwargs: {
            "state": STATE, "codeChallenge": "B" * 43,
            "codeChallengeMethod": "S256", "authorizationUrl": "https://accounts.google.com/x",
            "expiresAt": "2026-10-12T09:10:00.000Z",
        }
    )
    _with_service(application, service)

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.post(
            "/v1/google/authorization-transactions", headers=_auth(issued)
        )

    assert response.status_code == 201
    assert response.json()["state"] == STATE
    # 事务绑定发起设备：调用带的是设备 ID（位置参数，与参考实现一致）。
    name, kwargs, args = service.calls[0]
    assert name == "create_authorization_transaction"
    assert kwargs == {} and len(args) == 1


def test_unconfigured_oauth_reports_503(wired) -> None:
    """OAuth 未配置时创建事务返回 503。"""
    application, issued = wired
    _with_service(application, FakeGmailService(
        create_authorization_transaction=lambda *args, **kwargs: None
    ))

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.post(
            "/v1/google/authorization-transactions", headers=_auth(issued)
        )

    assert response.status_code == 503


@pytest.mark.parametrize("state", ["short", "A" * 44, "A" * 42, "bad!"])
def test_invalid_state_shape_is_rejected(wired, state: str) -> None:
    """**``state`` 必须是 43 个 URL 安全字符**——与生成时一致。"""
    application, issued = wired
    _with_service(application, FakeGmailService())

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.post(
            f"/v1/google/authorization-transactions/{state}/complete",
            json={"code": "c"}, headers=_auth(issued),
        )

    assert response.status_code == 400


@pytest.mark.parametrize("code", ["", "   ", 5, None])
def test_invalid_completion_codes_are_rejected(wired, code: object) -> None:
    """授权码必须是非空字符串。"""
    application, issued = wired
    _with_service(application, FakeGmailService())

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.post(
            f"/v1/google/authorization-transactions/{STATE}/complete",
            json={"code": code}, headers=_auth(issued),
        )

    assert response.status_code == 400


def test_completion_passes_the_state_from_the_path(wired) -> None:
    """``state`` 取自路径而不是正文。"""
    application, issued = wired
    service = FakeGmailService(
        complete_authorization_transaction=lambda *args, **kwargs: kwargs
    )
    _with_service(application, service)

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.post(
            f"/v1/google/authorization-transactions/{STATE}/complete",
            json={"code": "auth-code"}, headers=_auth(issued),
        )

    assert response.status_code == 200
    assert response.json()["state"] == STATE
    assert response.json()["authorization_code"] == "auth-code"


# --------------------------------------------------------------------------
# 连接与映射
# --------------------------------------------------------------------------


def test_lists_connections(wired) -> None:
    """列出连接。"""
    application, issued = wired
    _with_service(application, FakeGmailService(list_connections=lambda: [{"id": CONNECTION_ID}]))

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.get("/v1/google/connections", headers=_auth(issued))

    assert response.json() == {"connections": [{"id": CONNECTION_ID}]}


@pytest.mark.parametrize("identifier", ["not-a-uuid", "short", "1111"])
def test_invalid_connection_ids_are_rejected(wired, identifier: str) -> None:
    """连接 ID 必须是 UUID。"""
    application, issued = wired
    _with_service(application, FakeGmailService())

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.get(
            f"/v1/google/connections/{identifier}/mappings", headers=_auth(issued)
        )

    assert response.status_code == 400


def test_lists_mappings(wired) -> None:
    """列出映射。"""
    application, issued = wired
    service = FakeGmailService(list_mappings=lambda *args, **kwargs: [{"id": MAPPING_ID}])
    _with_service(application, service)

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.get(
            f"/v1/google/connections/{CONNECTION_ID}/mappings", headers=_auth(issued)
        )

    assert response.json() == {"mappings": [{"id": MAPPING_ID}]}
    # 位置调用（与参考实现的 `listMappings(connectionId)` 一致）。
    assert service.calls == [("list_mappings", {}, (CONNECTION_ID,))]


def test_invalid_mapping_id_is_rejected(wired) -> None:
    """映射 ID 必须是 UUID。"""
    application, issued = wired
    _with_service(application, FakeGmailService())

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.put(
            f"/v1/google/connections/{CONNECTION_ID}/mappings/not-a-uuid",
            json={"accountId": ACCOUNT_ID}, headers=_auth(issued),
        )

    assert response.status_code == 400


@pytest.mark.parametrize("account_id", [None, "not-a-uuid", 5])
def test_invalid_target_accounts_are_rejected(wired, account_id: object) -> None:
    """目标账户必须是 UUID。"""
    application, issued = wired
    _with_service(application, FakeGmailService())

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.put(
            f"/v1/google/connections/{CONNECTION_ID}/mappings/{MAPPING_ID}",
            json={"accountId": account_id}, headers=_auth(issued),
        )

    assert response.status_code == 400


def test_updates_a_mapping(wired) -> None:
    """改绑映射。"""
    application, issued = wired
    service = FakeGmailService(update_mapping=lambda *args, **kwargs: {"id": MAPPING_ID, **kwargs})
    _with_service(application, service)

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.put(
            f"/v1/google/connections/{CONNECTION_ID}/mappings/{MAPPING_ID}",
            json={"accountId": ACCOUNT_ID}, headers=_auth(issued),
        )

    assert response.status_code == 200
    assert service.calls == [("update_mapping", {
        "connection_id": CONNECTION_ID, "mapping_id": MAPPING_ID,
        "target_account_id": ACCOUNT_ID,
    }, ())]


# --------------------------------------------------------------------------
# 同步
# --------------------------------------------------------------------------


def test_sync_accepts_an_empty_body(wired) -> None:
    """**同步的所有参数都是可选的**，空正文是合法输入。"""
    application, issued = wired
    service = FakeGmailService(sync=lambda *args, **kwargs: {"mode": "incremental"})
    _with_service(application, service)

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.post(
            f"/v1/google/connections/{CONNECTION_ID}/sync", headers=_auth(issued)
        )

    assert response.status_code == 200
    assert service.calls == [("sync", {
        "connection_id": CONNECTION_ID, "confirmed_rescan_after_date": None,
        "explicit_account_id": None,
    }, ())]


def test_sync_passes_the_confirmed_rescan_date(wired) -> None:
    """**重扫起始日期由用户提供**，服务端不代选。"""
    application, issued = wired
    service = FakeGmailService(sync=lambda *args, **kwargs: kwargs)
    _with_service(application, service)

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        client.post(
            f"/v1/google/connections/{CONNECTION_ID}/sync",
            json={"confirmedRescanAfterDate": "2026-01-05", "explicitAccountId": ACCOUNT_ID},
            headers=_auth(issued),
        )

    assert service.calls[0][1]["confirmed_rescan_after_date"] == "2026-01-05"
    assert service.calls[0][1]["explicit_account_id"] == ACCOUNT_ID


@pytest.mark.parametrize("value", ["2026-1-5", "01/05/2026", "", "tomorrow"])
def test_invalid_rescan_dates_are_rejected(wired, value: str) -> None:
    """起始日期必须是 ``YYYY-MM-DD``。"""
    application, issued = wired
    _with_service(application, FakeGmailService())

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.post(
            f"/v1/google/connections/{CONNECTION_ID}/sync",
            json={"confirmedRescanAfterDate": value}, headers=_auth(issued),
        )

    assert response.status_code == 400


def test_sync_application_errors_keep_their_status(wired) -> None:
    """服务抛出的应用错误保留状态码与错误码。"""
    application, issued = wired

    def failing(*args, **kwargs):
        raise ApplicationError(
            code="GMAIL_RESYNC_CONFIRMATION_REQUIRED", message="需要确认",
            status_code=409, retryable=False,
        )

    _with_service(application, FakeGmailService(sync=failing))

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.post(
            f"/v1/google/connections/{CONNECTION_ID}/sync", headers=_auth(issued)
        )

    assert response.status_code == 409
    assert response.json()["code"] == "GMAIL_RESYNC_CONFIRMATION_REQUIRED"


# --------------------------------------------------------------------------
# 断开
# --------------------------------------------------------------------------


def test_disconnect_defaults_to_not_revoking(wired) -> None:
    """默认不向 Google 撤销授权。"""
    application, issued = wired
    service = FakeGmailService(disconnect=lambda *args, **kwargs: {"id": CONNECTION_ID})
    _with_service(application, service)

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.delete(
            f"/v1/google/connections/{CONNECTION_ID}", headers=_auth(issued)
        )

    assert response.status_code == 200
    assert service.calls == [("disconnect", {
        "connection_id": CONNECTION_ID, "revoke": False,
    }, ())]


def test_disconnect_can_revoke(wired) -> None:
    """正文 ``{"revoke": true}`` 时同时撤销授权。"""
    application, issued = wired
    service = FakeGmailService(disconnect=lambda *args, **kwargs: kwargs)
    _with_service(application, service)

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        # **`revoke` 来自正文**（参考实现的 disconnectSchema），不是查询串。
        # **`httpx` 的 `.delete()` 不接受 `json=`**，因此用 `request`。
        client.request(
            "DELETE", f"/v1/google/connections/{CONNECTION_ID}",
            headers=_auth(issued), json={"revoke": True},
        )

    assert service.calls[0][1]["revoke"] is True


def test_revocation_failure_is_reported(wired) -> None:
    """**撤销失败会上报**，不会静默地把连接标成已断开。"""
    application, issued = wired

    def failing(*args, **kwargs):
        raise ApplicationError(
            code="GMAIL_CONNECTION_ERROR", message="Google 未确认撤销",
            status_code=503, retryable=True,
        )

    _with_service(application, FakeGmailService(disconnect=failing))

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.delete(
            f"/v1/google/connections/{CONNECTION_ID}?revoke=true", headers=_auth(issued)
        )

    assert response.status_code == 503


def test_disconnect_reads_revoke_from_the_body_not_the_query(wired) -> None:
    """**``revoke`` 只从正文读取**；查询串里的同名参数被忽略。

    此前它读的是 ``?revoke=true``。一个按参考实现发正文的客户端会被**静默地**按
    "不撤销"处理：**连接确实断开了，但 Google 侧的授权仍然有效**。

    这类"部分生效且不报错"的差异比直接失败更难发现——因此值得单独固定一条测试：
    **查询串形式必须不再生效**，否则旧路径会被无意中保留下来。
    """
    application, issued = wired
    seen: dict = {}
    service = FakeGmailService(disconnect=lambda *args, **kwargs: seen.update(kwargs))
    _with_service(application, service)

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        client.delete(
            f"/v1/google/connections/{CONNECTION_ID}?revoke=true", headers=_auth(issued)
        )

    assert seen.get("revoke") is False, "查询串里的 revoke 不应生效"


def test_disconnect_defaults_revoke_to_false(wired) -> None:
    """省略正文时按 ``false`` 处理（参考实现的 schema 有 ``default(false)``）。"""
    application, issued = wired
    seen: dict = {}
    service = FakeGmailService(disconnect=lambda *args, **kwargs: seen.update(kwargs))
    _with_service(application, service)

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        client.delete(f"/v1/google/connections/{CONNECTION_ID}", headers=_auth(issued))

    assert seen.get("revoke") is False


# --------------------------------------------------------------------------
# 用户范围与停用
# --------------------------------------------------------------------------


def _valid_requests() -> list[tuple[str, str, dict]]:
    """7 条路由各一份**参数合法**的请求，用于验证它们都走到了用户绑定视图。"""
    return [
        ("post", "/v1/google/authorization-transactions", {}),
        ("post", f"/v1/google/authorization-transactions/{STATE}/complete", {"code": "c"}),
        ("get", "/v1/google/connections", {}),
        ("get", f"/v1/google/connections/{CONNECTION_ID}/mappings", {}),
        ("put", f"/v1/google/connections/{CONNECTION_ID}/mappings/{MAPPING_ID}",
         {"accountId": ACCOUNT_ID}),
        ("post", f"/v1/google/connections/{CONNECTION_ID}/sync", {}),
        ("delete", f"/v1/google/connections/{CONNECTION_ID}", {}),
    ]


def _perform(client, method: str, path: str, body: dict, headers: dict):
    """发一个请求；**``httpx`` 的 ``.delete()`` 不接受 ``json=``**，因此统一走 ``request``。"""
    if method == "get":
        return client.get(path, headers=headers)
    return client.request(method.upper(), path, json=body, headers=headers)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [pytest.param(*case, id=f"{case[0]} {case[1]}") for case in _valid_requests()],
)
def test_every_route_uses_the_authenticated_user(
    wired, method: str, path: str, body: dict
) -> None:
    """**每条业务动作都在 ``for_user(当前用户)`` 的视图上执行。**

    用户来自访问令牌解析出的 ``request.state.auth.user_id``，不是请求参数。
    """
    application, issued = wired
    service = FakeGmailService()
    _with_service(application, service)

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = _perform(client, method, path, body, _auth(issued))

    assert response.status_code < 400, response.text
    assert service.scoped_user_ids == [issued.user.id], path


def test_the_user_scope_ignores_a_user_id_in_the_body(wired) -> None:
    """**正文里的 ``userId`` 不能指定归属**（计划 §4.2）。

    若路由读正文，任何已配对设备都能声明自己属于别人。
    """
    application, issued = wired
    service = FakeGmailService()
    _with_service(application, service)

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.put(
            f"/v1/google/connections/{CONNECTION_ID}/mappings/{MAPPING_ID}",
            json={"accountId": ACCOUNT_ID, "userId": "99999999-9999-9999-9999-999999999999"},
            headers=_auth(issued),
        )

    assert response.status_code == 200
    assert service.scoped_user_ids == [issued.user.id]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [pytest.param(*case, id=f"{case[0]} {case[1]}") for case in _valid_requests()],
)
def test_disabled_user_is_rejected_on_every_route(
    wired, method: str, path: str, body: dict
) -> None:
    """**停用用户的 Gmail 请求在入队处就被拒绝**（403 ``USER_DISABLED``）。

    这是"停用后的排队同步不能入账"的第一道闸门：请求根本进不到服务层，
    因此也不会继续拉取或写回（计划 §4.4）。
    """
    application, issued = wired
    application.state.database.connection.execute(
        "UPDATE users SET status = 'disabled' WHERE id = CAST(? AS UUID)",
        [issued.user.id],
    )

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = _perform(client, method, path, body, _auth(issued))

    assert response.status_code == 403, path
    assert response.json()["code"] == "USER_DISABLED"


# --------------------------------------------------------------------------
# 真实服务的跨用户隔离
# --------------------------------------------------------------------------


def _seed_connection(application, *, user_id: str, subject: str, email: str) -> str:
    """直接写入一条连接，用于验证真实服务的读取范围。"""
    connection_id = str(uuid.uuid4())
    application.state.database.connection.execute(
        """
        INSERT INTO gmail_connections (
            id, user_id, google_subject, google_email, encrypted_refresh_token,
            granted_scopes, status
        ) VALUES (
            CAST(? AS UUID), CAST(? AS UUID), ?, ?, 'v1.iv.ct.tag', 'scope',
            'pending_initial_scan'
        )
        """,
        [connection_id, user_id, subject, email],
    )
    return connection_id


def _seed_user(application, name: str) -> str:
    """建立第二个用户。"""
    user_id = str(uuid.uuid4())
    application.state.database.connection.execute(
        "INSERT INTO users (id, display_name) VALUES (CAST(? AS UUID), ?)",
        [user_id, name],
    )
    return user_id


def test_real_service_lists_only_the_authenticated_users_connections(wired) -> None:
    """**A 列不出 B 的连接**，但自己的连接正常可见（V09）。"""
    application, issued = wired
    own = _seed_connection(
        application, user_id=issued.user.id, subject="own", email="own@example.com"
    )
    other_user = _seed_user(application, "User B")
    _seed_connection(
        application, user_id=other_user, subject="other", email="other@example.com"
    )

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.get("/v1/google/connections", headers=_auth(issued))

    assert response.status_code == 200
    assert [row["id"] for row in response.json()["connections"]] == [own]


def test_real_service_cannot_read_another_users_mappings(wired) -> None:
    """**A 读不到 B 连接的映射**：返回空列表，与"没有映射"不可区分。"""
    application, issued = wired
    other_user = _seed_user(application, "User B")
    foreign = _seed_connection(
        application, user_id=other_user, subject="other", email="other@example.com"
    )

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.get(
            f"/v1/google/connections/{foreign}/mappings", headers=_auth(issued)
        )

    assert response.status_code == 200
    assert response.json() == {"mappings": []}


def test_real_service_cannot_disconnect_another_users_connection(wired) -> None:
    """**A 断不开 B 的连接**：404，与"连接不存在"一致；B 的连接保持原状（V09）。"""
    application, issued = wired
    other_user = _seed_user(application, "User B")
    foreign = _seed_connection(
        application, user_id=other_user, subject="other", email="other@example.com"
    )

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.delete(
            f"/v1/google/connections/{foreign}", headers=_auth(issued)
        )

    assert response.status_code == 404
    row = application.state.database.connection.execute(
        "SELECT status FROM gmail_connections WHERE id = CAST(? AS UUID)", [foreign]
    ).fetchone()
    assert row is not None and row[0] == "pending_initial_scan"


def test_real_service_cannot_sync_another_users_connection(wired) -> None:
    """**A 同步不了 B 的连接**：真实服务在读取连接时按用户范围过滤，返回 404。

    这条走的是真实同步路径（``sync`` → ``load_connection``），不是桩：连接读不到时
    同步在刷新令牌与拉取消息之前就终止（V09 的"同步"一项）。
    """
    application, issued = wired
    other_user = _seed_user(application, "User B")
    foreign = _seed_connection(
        application, user_id=other_user, subject="other", email="other@example.com"
    )

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.post(
            f"/v1/google/connections/{foreign}/sync", headers=_auth(issued)
        )

    assert response.status_code == 404
    assert response.json()["code"] == "GMAIL_CONNECTION_ERROR"


def test_real_service_reports_404_for_an_unknown_connection(wired) -> None:
    """自己名下不存在的连接同样 404——与"别人的连接"不可区分。"""
    application, issued = wired

    with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
        response = client.delete(
            f"/v1/google/connections/{CONNECTION_ID}", headers=_auth(issued)
        )

    assert response.status_code == 404


def test_real_service_refuses_to_sync_after_the_user_is_disabled(wired) -> None:
    """**停用提交后，已经在执行/排队的同步在实际执行点被拒绝**（V11 的 Gmail 侧）。

    这里直接调用真实服务而不是发请求，是为了模拟"请求已通过入队检查（当时用户仍是
    active），随后被停用"的时序：``require_user`` 拦不住这种在飞请求，唯一可靠的闸门
    是执行点本身——同步读取连接时先确认用户仍活动，因此**在刷新令牌、拉取消息与
    写回任何状态之前**就终止（计划 §4.4）。
    """
    application, issued = wired
    connection_id = _seed_connection(
        application, user_id=issued.user.id, subject="own", email="own@example.com"
    )
    service = application.state.gmail_service.for_user(issued.user.id)

    # 对照：用户仍活动时，这一次调用走到"必须确认重扫范围"（409）——
    # 说明连接确实读到了，403 不是别的原因造成的。
    with pytest.raises(ApplicationError) as active:
        service.sync(
            connection_id=connection_id, confirmed_rescan_after_date=None,
            explicit_account_id=None,
        )
    assert active.value.code == "GMAIL_RESYNC_CONFIRMATION_REQUIRED"

    application.state.database.connection.execute(
        "UPDATE users SET status = 'disabled' WHERE id = CAST(? AS UUID)",
        [issued.user.id],
    )

    with pytest.raises(ApplicationError) as raised:
        service.sync(
            connection_id=connection_id, confirmed_rescan_after_date=None,
            explicit_account_id=None,
        )

    assert raised.value.code == "USER_DISABLED"
    assert raised.value.status_code == 403
    # 连接状态没有被写回（没有 last_sync_at、状态未变）。
    row = application.state.database.connection.execute(
        "SELECT status, last_sync_at FROM gmail_connections WHERE id = CAST(? AS UUID)",
        [connection_id],
    ).fetchone()
    assert row is not None
    assert row[0] == "pending_initial_scan"
    assert row[1] is None
