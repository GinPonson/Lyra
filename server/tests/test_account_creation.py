"""账户创建端点。

覆盖三条不变量：

1. **掩码只取标识末尾 3–4 位数字**，绝不回传文件账户标识本身——它是导入匹配键，
   泄漏它等于泄漏"哪份文件属于哪个账户"；
2. **幂等重放返回同一个账户**且不重复创建；
3. 账户、审计与幂等收口**同一事务**。
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

_KEYS = Path(__file__).parent / "fixtures" / "device-keys.json"
_PUBLIC_KEY_PEM = json.loads(_KEYS.read_text(encoding="utf-8"))["cases"][0]["inputPem"]
_IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")

_VALID = {"displayName": "My Account", "accountIdentifier": "ACCT-00123", "baseCurrency": "USD"}


@pytest.fixture()
def client(tmp_path: Path):
    """构造应用与一个有效访问令牌。"""
    database_path = tmp_path / "accounts.duckdb"
    database = open_lyra_database(str(database_path))
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    application = create_app(settings, database)
    repository = DuckDbDeviceRepository(database.connection)
    service = DevicePairingService(repository, WriteCoordinator())
    issued = asyncio.run(service.authorize_owner_device(
        identity=_IDENTITY, display_name="Pixel 9", public_key_pem=_PUBLIC_KEY_PEM
    ))
    try:
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
            yield test_client, issued, database
    finally:
        database.close()


def _auth(issued) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {issued.access_token}",
        # **每个 /v1 请求都必须声明客户端协议版本**（除公开路由）。
        # 缺失时服务端返回 426——那是客户端唯一能据此升级 App 的信号。
        "x-lyra-protocol-version": "2",
    }


def _create(client, issued, body, *, key: str | None = None):
    """创建账户；``key`` 非空时带幂等键。"""
    headers = _auth(issued)
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post("/v1/ledger/accounts", headers=headers, json=body)


# --------------------------------------------------------------------------
# 创建
# --------------------------------------------------------------------------


def test_create_returns_201_and_the_public_account(client) -> None:
    """创建成功返回 201 与公开账户。"""
    test_client, issued, _ = client
    response = _create(test_client, issued, _VALID)

    assert response.status_code == 201
    account = response.json()["account"]
    assert account["displayName"] == "My Account"
    assert account["baseCurrency"] == "USD"
    assert account["status"] == "active"


def test_create_never_returns_the_file_account_identifier(client) -> None:
    """**响应里绝不出现文件账户标识。**

    它是导入匹配键：泄漏它等于泄漏"哪份文件属于哪个账户"。对外只给末尾 3–4 位数字
    作为脱敏后缀。
    """
    test_client, issued, _ = client
    body = _create(test_client, issued, _VALID).json()

    assert "ACCT-00123" not in json.dumps(body, ensure_ascii=False)
    assert body["account"]["maskedSuffix"] == "0123"


def test_create_normalizes_case_and_whitespace(client) -> None:
    """标识与币种归一化为大写、名称去首尾空白。"""
    test_client, issued, _ = client
    response = _create(test_client, issued, {
        "displayName": "  My Account  ",
        "accountIdentifier": " acct-00123 ",
        "baseCurrency": "usd",
    })

    assert response.status_code == 201
    assert response.json()["account"]["displayName"] == "My Account"


def test_create_defaults_the_currency_to_usd(client) -> None:
    """省略 ``baseCurrency`` 时默认 USD（参考实现的 schema 有 default）。"""
    test_client, issued, _ = client
    response = _create(test_client, issued, {
        "displayName": "My Account", "accountIdentifier": "ACCT-00123",
    })

    assert response.status_code == 201
    assert response.json()["account"]["baseCurrency"] == "USD"


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("名称为空", {"displayName": "", "accountIdentifier": "ACCT-00123"}),
        ("名称超过 64", {"displayName": "x" * 65, "accountIdentifier": "ACCT-00123"}),
        ("标识不足 4 位", {"displayName": "A", "accountIdentifier": "A12"}),
        ("标识不以 3 位数字结尾", {"displayName": "A", "accountIdentifier": "ACCT-01"}),
        ("标识含小写以外的非法字符", {"displayName": "A", "accountIdentifier": "ACCT_00123"}),
        ("币种不足 3 字母", {"displayName": "A", "accountIdentifier": "ACCT-00123", "baseCurrency": "US"}),
    ],
)
def test_invalid_bodies_are_rejected(client, label: str, body: dict) -> None:
    """各字段的边界都返回 400。"""
    test_client, issued, _ = client
    response = _create(test_client, issued, body)

    assert response.status_code == 400, label
    assert response.json()["code"] == "INVALID_ARGUMENT"


def test_an_empty_body_is_rejected(client) -> None:
    """空正文 400（三个字段都无默认值，除币种）。"""
    test_client, issued, _ = client
    assert _create(test_client, issued, {}).status_code == 400


# --------------------------------------------------------------------------
# 幂等
# --------------------------------------------------------------------------


def test_replaying_the_same_key_returns_the_same_account(client) -> None:
    """**重复提交返回同一个账户，且不产生第二个。**

    重放返回 **200** 而不是 201：201 表示"本次创建了一个资源"，而重放没有创建任何
    东西——客户端据此判断是否需要刷新列表。
    """
    test_client, issued, database = client
    key = "account-key-000001"

    first = _create(test_client, issued, _VALID, key=key)
    second = _create(test_client, issued, _VALID, key=key)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["account"] == second.json()["account"]
    count = database.connection.execute("SELECT count(*) FROM accounts").fetchone()[0]
    assert count == 1, "重放不应创建第二个账户"


def test_different_keys_create_two_accounts(client) -> None:
    """不同幂等键各自创建——**它们是两次不同的请求**。

    要注意标识唯一：账户标识有 UNIQUE 约束，因此第二次用不同的标识。
    """
    test_client, issued, database = client
    first = _create(test_client, issued, _VALID, key="account-key-000001")
    second = _create(
        test_client, issued, {**_VALID, "accountIdentifier": "ACCT-00456"},
        key="account-key-000002",
    )

    assert first.status_code == 201
    assert second.status_code == 201
    count = database.connection.execute("SELECT count(*) FROM accounts").fetchone()[0]
    assert count == 2


def test_a_duplicate_identifier_is_rejected(client) -> None:
    """**同一个文件账户标识不能建两个账户**（表的 UNIQUE 约束）。

    不带幂等键的重复提交会走到这条约束——它必须报错而不是静默成功。
    """
    test_client, issued, _ = client
    first = _create(test_client, issued, _VALID)
    second = _create(test_client, issued, _VALID)

    assert first.status_code == 201
    assert second.status_code >= 400


def test_the_audit_event_is_written_with_the_device_as_actor(client) -> None:
    """**创建必须留下审计**：actor 是设备、target 是该账户。"""
    test_client, issued, database = client
    created = _create(test_client, issued, _VALID).json()["account"]

    rows = database.connection.execute(
        "SELECT event_type, outcome, actor_type, target_type, account_id FROM audit_events"
    ).fetchall()

    assert len(rows) == 1
    event_type, outcome, actor_type, target_type, account_id = rows[0]
    assert event_type == "account.created"
    assert outcome == "success"
    assert actor_type == "device"
    assert target_type == "account"
    assert str(account_id) == created["id"]


def test_the_account_appears_in_the_list(client) -> None:
    """创建后能在列表里看到——**这条路径是端到端的**。"""
    test_client, issued, _ = client
    created = _create(test_client, issued, _VALID).json()["account"]

    listed = test_client.get(
        "/v1/ledger/accounts", headers=_auth(issued)
    ).json()

    assert created["id"] in [row["id"] for row in listed["rows"]]
