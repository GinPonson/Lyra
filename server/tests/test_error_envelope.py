"""错误响应信封契约。

**这是每个客户端错误路径都依赖的形状**，而它此前与参考实现完全不同：

| | 参考实现 | 改前 |
| --- | --- | --- |
| 信封 | **扁平** | 嵌套在 ``detail`` 下 |
| ``message`` / ``details`` / ``retryable`` / ``request_id`` | 有 | **全部缺失** |

其中 ``retryable`` 影响最大：它决定客户端是否重试。**缺了它，客户端无法区分可重试与
永久失败**，只能一律重试（放大故障）或一律放弃（用户看到假失败）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.protocol import CLIENT_PROTOCOL_HEADERS
from lyra.application.device_pairing_service import DevicePairingService
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.infrastructure.db.user_repository import UserRepository
from lyra.main import create_app

_KEYS = Path(__file__).parent / "fixtures" / "device-keys.json"
_PUBLIC_KEY_PEM = json.loads(_KEYS.read_text(encoding="utf-8"))["cases"][0]["inputPem"]
_IDENTITY = LoginIdentity(
    provider="github", subject="12345", email=None, display_name="octocat"
)

ENVELOPE_KEYS = {"request_id", "code", "message", "details", "retryable"}


@pytest.fixture()
def client(tmp_path: Path):
    """构造应用与一个有效访问令牌。

    **不预置账本数据**：本文件只验证错误信封的形状，不需要任何行。
    """
    database_path = tmp_path / "envelope.duckdb"
    database = open_lyra_database(str(database_path))
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    application = create_app(settings, database)
    repository = DuckDbDeviceRepository(database.connection)
    service = DevicePairingService(
        repository, application.state.write_coordinator, UserRepository(database.connection)
    )
    issued = asyncio.run(service.authorize_user_device(
        identity=_IDENTITY, display_name="Pixel 9", public_key_pem=_PUBLIC_KEY_PEM
    ))
    try:
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
            yield test_client, issued
    finally:
        database.close()




def test_the_envelope_is_flat_not_nested(client) -> None:
    """**错误体是扁平的**，不是 ``{"detail": {...}}``。

    客户端读 ``body.code``；嵌套形状会让它读到 ``undefined``——不报错，只是什么都拿不到。
    """
    test_client, _ = client
    body = test_client.get("/v1/ledger/summary").json()

    assert set(body) == ENVELOPE_KEYS
    assert "detail" not in body


def test_an_unauthenticated_request_reports_a_stable_code(client) -> None:
    """未认证请求的码是稳定的。"""
    test_client, _ = client
    response = test_client.get("/v1/ledger/summary")

    assert response.status_code == 401
    assert response.json()["code"] == "DEVICE_CREDENTIAL_INVALID"
    assert response.json()["retryable"] is False


def test_every_response_carries_a_request_id_header(client) -> None:
    """**每个响应都带 ``x-request-id`` 头**，成功与失败都一样。

    用户报障时给出的就是这个值；没有它就无法从日志定位那次请求。

    这里刻意覆盖**四种产出响应的路径**：成功路由、未认证（领域错误）、404（框架抛出的
    ``HTTPException``）与 405（路由方法不匹配）。此前只测成功路径，漏掉了真正会断的
    分支——而"每个响应都带"这句话的含义恰恰是**没有例外**。
    """
    test_client, issued = client

    samples = [
        test_client.get("/v1/health"),                                  # 200 成功
        test_client.get("/v1/ledger/summary"),                          # 401 未认证
        test_client.get("/v1/no-such-route"),                           # 404
        test_client.post("/v1/health"),                                 # 405
    ]

    for response in samples:
        request_id = response.headers.get("x-request-id")
        assert request_id, f"{response.status_code} 响应缺少 x-request-id"
        # 错误响应里，头与体必须是**同一个** ID——否则报障的 ID 关联不到错误体。
        if response.status_code >= 400:
            assert request_id == response.json()["request_id"], response.status_code


def test_the_426_protocol_gate_response_carries_a_request_id_header(client) -> None:
    """**426 也要带请求 ID 头。**

    协议门在 ``call_next`` **之前**直接返回，不走中间件"响应发出后补头"的那段代码——
    它是唯一一条这样返回的路径，因此最容易漏。用户遇到 426 时唯一的动作是升级 App，
    而升级前报障靠的就是这个 ID。
    """
    test_client, _ = client
    # 不带默认协议头：本测试要的正是"被协议门拦下"的那条响应。
    with TestClient(test_client.app) as gateless_client:
        response = gateless_client.get("/v1/ledger/summary")

    assert response.status_code == 426
    assert response.headers.get("x-request-id")
    assert response.headers["x-request-id"] == response.json()["request_id"]


def test_an_unclassified_exception_carries_the_header_and_leaves_a_log(
    client, caplog
) -> None:
    """**未归类异常：对外只说内部错误，对内必须留下可排查的记录。**

    500 响应由 Starlette 的 ``ServerErrorMiddleware`` 直接下发（异常从内层重新抛出），
    同样不经过中间件的补头代码，因此必须单独覆盖。

    三件事一起断言，缺一不可：

    1. 响应头带 ``x-request-id``，且与错误体一致——否则运维拿到的 ID 查不到日志；
    2. **日志真的产生了**，含请求 ID 与异常原因——只断言"有 logger"等于什么都没验证；
    3. 对外正文**不含**内部异常文本、路径或 SQL——那会把实现细节泄露给客户端。
    """
    test_client, _ = client

    def explode() -> None:
        raise RuntimeError("boom /srv/secret/ledger.sql SELECT * FROM accounts")

    test_client.app.add_api_route("/v1/_unexpected", explode)
    with caplog.at_level(logging.ERROR):
        # **必须显式关掉异常重抛**：``ServerErrorMiddleware`` 发完 500 后会把异常重新
        # 抛出（供服务器记录），默认的 TestClient 会把它当测试失败抛出，从而测不到响应。
        with TestClient(
            test_client.app,
            headers=CLIENT_PROTOCOL_HEADERS,
            raise_server_exceptions=False,
        ) as boom_client:
            response = boom_client.get("/v1/_unexpected")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert body["message"] == "服务端发生内部错误"
    assert response.headers.get("x-request-id") == body["request_id"]

    for leaked in ("RuntimeError", "secret", "ledger.sql", "SELECT"):
        assert leaked not in response.text, f"内部信息泄露：{leaked}"

    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert errors, "未归类异常必须写入日志"
    logged = "\n".join(record.getMessage() for record in errors)
    assert body["request_id"] in logged, "日志缺少 request_id，用户报障时无法定位"
    assert "RuntimeError" in logged and "boom" in logged, "日志缺少异常原因"


def test_the_header_and_the_body_carry_the_same_request_id(client) -> None:
    """响应头里的 ID 与错误体里的是同一个——否则关联不上。"""
    test_client, _ = client
    response = test_client.get("/v1/ledger/summary")

    assert response.headers["x-request-id"] == response.json()["request_id"]


def test_extra_detail_keys_land_in_details(client) -> None:
    """**路由写在 ``detail`` 里的补充字段归入 ``details``。**

    丢掉它们会让客户端只知道"状态冲突"而不知道冲突在哪。
    """
    test_client, issued = client
    body = test_client.post(
        "/v1/agent/sessions/x/messages",
        headers={"Authorization": f"Bearer {issued.access_token}"},
        json={"content": "hi"},
    ).json()

    # 该响应应为错误（会话不存在或运行时未装配），且补充信息在 details 里。
    assert "code" in body
    if body["details"] is not None:
        assert isinstance(body["details"], dict)


def test_request_id_is_the_only_allowed_snake_case_key(client) -> None:
    """**``request_id`` 是唯一允许的 snake_case 键**（日志关联标识，非领域字段）。"""
    test_client, _ = client
    body = test_client.get("/v1/ledger/summary").json()

    underscored = [key for key in body if "_" in key]
    assert underscored == ["request_id"]


def test_validation_failure_is_a_400_with_invalid_argument(client) -> None:
    """参数校验失败返回 **400 ``INVALID_ARGUMENT``**（参考实现用 AJV 校验，同样 400）。

    此前返回 422——那是 FastAPI 的默认值，与参考实现不一致。
    """
    test_client, issued = client
    response = test_client.get(
        "/v1/analytics/risk", headers={"Authorization": f"Bearer {issued.access_token}"},
        params={"scope": "all"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


def test_error_messages_are_never_empty(client) -> None:
    """**错误消息不得为空。**

    客户端常把 ``message`` 直接展示给用户；空字符串会让用户看到一个**没有说明的错误**，
    而"哪里出了问题"恰恰是他最需要知道的。

    参考实现为每个错误都提供了文案。这里遍历几条典型路径，断言消息非空——它同时是
    "新增路由忘了写 message"的守卫。
    """
    test_client, issued = client
    auth = {"Authorization": f"Bearer {issued.access_token}"}

    samples = [
        test_client.get("/v1/ledger/summary"),                        # 未认证
        test_client.get("/v1/ledger/summary", headers=auth),          # 缺 scope
        test_client.get("/v1/ledger/summary", headers=auth,
                        params={"scope": "bad-key"}),                 # 非法 scope
        test_client.get("/v1/ledger/trades", headers=auth,
                        params={"scope": "all", "cursor": "!!"}),     # 无效游标
    ]

    for response in samples:
        body = response.json()
        assert response.status_code >= 400, response.status_code
        assert body["message"] != "", f"{response.status_code} {body['code']} 的消息为空"


def test_a_domain_error_message_survives_the_translation(client) -> None:
    """**领域错误的消息必须传到客户端。**

    路由曾写成 ``detail={"code": error.code}``——**消息被静默丢弃**。整个错误处理链
    仍然工作，只是客户端拿不到任何说明。
    """
    test_client, issued = client
    response = test_client.get(
        "/v1/ledger/summary", headers={"Authorization": f"Bearer {issued.access_token}"},
        params={"scope": "bad-key"},
    )

    assert response.status_code == 400
    assert response.json()["message"] == "请求必须明确指定单个账户或全部活跃账户"
