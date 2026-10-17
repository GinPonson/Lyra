"""账本与导入的跨用户隔离（验收 V03 / V04 / V05）。

这个文件回答的是**端到端**问题，而不是单点断言：两个用户 A / B 拥有**完全相同的
legacy account key**（``TST001``）、上传**完全相同的文件**，能不能各自正确导入、
各自看到自己的账本、并且互相不能读写对方的资源。

之所以要单独成文件：契约 §3.2 明确 ``transactions`` / ``lots`` / ``realized`` 这些表
**刻意没有 ``user_id`` 列**，它们的跨用户保护完全落在应用层的查询范围上。因此这些
表的隔离不能靠"约束存在"来证明，只能在真实 HTTP 路径上逐个场景撞。

用户与设备通过 ``UserRepository`` + ``DevicePairingService``（P2 落地）真实创建，
不使用桩——桩无法证明隔离，计划 §5 P3 的验收门槛也明确禁止以桩充当 V03–V06 证据。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.application.device_pairing_service import DevicePairingService
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.infrastructure.db.user_repository import UserRepository
from lyra.main import create_app
from lyra.protocol import CLIENT_PROTOCOL_HEADERS

PUBLIC_KEY_PEM = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"][0]["inputPem"]

IDENTITY_A = LoginIdentity(provider="github", subject="isolation-a", email=None, display_name="A")
IDENTITY_B = LoginIdentity(provider="github", subject="isolation-b", email=None, display_name="B")

ACCOUNT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
ACCOUNT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
UNKNOWN_ACCOUNT = "99999999-9999-4999-8999-999999999999"
UNKNOWN_JOB = "99999999-9999-4999-8999-999999999998"

# **两个用户用同一个账户标识**——这正是 schema 16「用户内唯一」的改造目标。
LEGACY_KEY = "TST001"

CSV_HEADER = "Date,Action,Symbol,Description,Quantity,Price,Fees & Comm,Amount\n"


def _csv(date: str, amount: str, price: str = "$40.00", quantity: str = "10") -> bytes:
    """一份最小但合法的 Schwab 八列 CSV；行内容不同 → 哈希不同。"""
    return (
        CSV_HEADER
        + f'"{date}","Buy","INTC","INTEL CORP","{quantity}","{price}","","{amount}"\n'
    ).encode()


def _file_name(suffix: str = "20260105") -> str:
    """文件名里的 ``TST001`` 是账户线索的来源，两个用户同用一个名字。"""
    return f"Individual_{LEGACY_KEY}_Transactions_{suffix}.csv"


@pytest.fixture()
def wired(tmp_path: Path):
    """两个用户各一台设备、各一个同键账户的真实应用。"""
    database_path = tmp_path / "isolation.duckdb"
    database = open_lyra_database(str(database_path))
    users = UserRepository(database.connection)
    now = datetime.now(UTC)
    user_a = users.find_or_create_user_for_identity(IDENTITY_A, now=now).user.id
    user_b = users.find_or_create_user_for_identity(IDENTITY_B, now=now).user.id
    for account_id, user_id in ((ACCOUNT_A, user_a), (ACCOUNT_B, user_b)):
        database.connection.execute(
            """
            INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Account', ?, '001')
            """,
            [account_id, user_id, LEGACY_KEY],
        )

    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    application = create_app(settings, database)
    pairing = DevicePairingService(
        DuckDbDeviceRepository(database.connection),
        application.state.write_coordinator,
        users,
    )
    issued_a = asyncio.run(pairing.authorize_user_device(
        identity=IDENTITY_A, display_name="A Phone", public_key_pem=PUBLIC_KEY_PEM
    ))
    issued_b = asyncio.run(pairing.authorize_user_device(
        identity=IDENTITY_B, display_name="B Phone", public_key_pem=PUBLIC_KEY_PEM
    ))
    try:
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as test_client:
            yield test_client, issued_a, issued_b, user_a, user_b
    finally:
        database.close()


def _auth(issued) -> dict[str, str]:
    return {"Authorization": f"Bearer {issued.access_token}", **CLIENT_PROTOCOL_HEADERS}


def _preview(client, issued, content: bytes, name: str | None = None):
    """上传一份文件并返回预览响应。"""
    return client.post(
        "/v1/imports/preview",
        content=content,
        headers={
            **_auth(issued),
            "x-lyra-file-name": name or _file_name(),
            "content-type": "application/octet-stream",
        },
    )


def _commit(client, issued, import_id: str, token: str, key: str | None = None):
    headers = _auth(issued)
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(
        f"/v1/imports/{import_id}/commit", json={"confirmToken": token}, headers=headers
    )


def _error_payload(response) -> dict:
    """去掉 ``request_id`` 后比较错误语义。

    每次请求的 ``request_id`` 天然不同（这是错误信封的追踪字段），拿整个响应体比较会
    永远失败；要证明的是「两种情形的错误码与细节不可区分」。
    """
    body = response.json()
    assert "request_id" in body
    return {key: value for key, value in body.items() if key != "request_id"}


def _summary(client, issued, account_id: str) -> dict:
    """读取一个账户范围的总览信封，返回原始 JSON。"""
    response = client.get(
        "/v1/ledger/summary", headers=_auth(issued), params={"scope": account_id}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _transaction_count(client, issued, account_id: str) -> int:
    """从总览信封里读交易笔数，用于跨用户对照。"""
    body = _summary(client, issued, account_id)
    total = 0
    for group in body["data"]["groups"]:
        total += group["data"]["transactionCount"]
    return total


# --------------------------------------------------------------------------
# V03：同键账户 + scope=all
# --------------------------------------------------------------------------


def test_scope_all_only_covers_the_current_user(wired) -> None:
    """两个用户各有同键账户时，``scope=all`` 各自只含本人。

    契约 §3.11 点名 ``scope=all`` 以前"不加任何条件"；这条断言把"该用户的全部账户"
    钉死在 HTTP 层，而不是只钉在仓储层。
    """
    client, issued_a, issued_b, _, _ = wired

    all_a = client.get("/v1/ledger/summary", headers=_auth(issued_a), params={"scope": "all"})
    all_b = client.get("/v1/ledger/summary", headers=_auth(issued_b), params={"scope": "all"})

    assert all_a.status_code == 200 and all_b.status_code == 200
    assert all_a.json()["metadata"]["accountIds"] == [ACCOUNT_A]
    assert all_b.json()["metadata"]["accountIds"] == [ACCOUNT_B]


# --------------------------------------------------------------------------
# V03：账户创建与清单按用户
# --------------------------------------------------------------------------


def _create_account(client, issued, identifier: str, display_name: str = "My Account"):
    return client.post(
        "/v1/ledger/accounts",
        headers=_auth(issued),
        json={"displayName": display_name, "accountIdentifier": identifier, "baseCurrency": "USD"},
    )


def test_the_same_account_identifier_can_coexist_across_users(wired) -> None:
    """同一个文件账户标识在两个用户下都能创建；同一用户重复创建仍然 409。

    账户标识的唯一键已从全局改为 ``(user_id, legacy_account_key)``。重复检测若不带
    用户范围，B 的创建会被 A 的同名账户挡成 409——A/B 用相同标识共存正是本次改造的
    目标，而"同用户内重复"这条既有的保护必须同时保留。
    """
    client, issued_a, issued_b, _, _ = wired

    created_a = _create_account(client, issued_a, "ACCT-00123", "A Account")
    created_b = _create_account(client, issued_b, "ACCT-00123", "B Account")
    assert created_a.status_code == 201, created_a.text
    assert created_b.status_code == 201, created_b.text
    assert created_a.json()["account"]["id"] != created_b.json()["account"]["id"]

    duplicate = _create_account(client, issued_a, "ACCT-00123", "A Account Again")
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "ACCOUNT_ALREADY_EXISTS"


def test_account_list_shows_only_the_current_users_accounts(wired) -> None:
    """账户清单不得带出别人的账户（展示名、券商、脱敏后缀都算用户数据）。"""
    client, issued_a, issued_b, _, _ = wired
    _create_account(client, issued_a, "ACCT-00123", "A Account")
    _create_account(client, issued_b, "ACCT-00222", "B Account")

    list_a = client.get("/v1/ledger/accounts", headers=_auth(issued_a), params={"scope": "all"})
    list_b = client.get("/v1/ledger/accounts", headers=_auth(issued_b), params={"scope": "all"})

    assert list_a.status_code == 200 and list_b.status_code == 200
    names_a = {row["displayName"] for row in list_a.json()["data"]["rows"]}
    names_b = {row["displayName"] for row in list_b.json()["data"]["rows"]}
    assert names_a == {"Account", "A Account"}
    assert names_b == {"Account", "B Account"}


def test_another_users_account_id_is_the_same_404_as_a_missing_one(wired) -> None:
    """传别人的 ``accountId`` 与传一个不存在的 UUID 返回**完全相同**的 404。

    区分两者等于提供一个人人可调的"账户是否存在"探针；用空账本掩盖越权则会误导
    用户以为"该账户没有交易"。
    """
    client, issued_a, issued_b, _, _ = wired

    other = client.get(
        "/v1/ledger/summary", headers=_auth(issued_b), params={"scope": ACCOUNT_A}
    )
    missing = client.get(
        "/v1/ledger/summary", headers=_auth(issued_b), params={"scope": UNKNOWN_ACCOUNT}
    )

    assert other.status_code == 404
    assert missing.status_code == 404
    assert _error_payload(other) == _error_payload(missing)
    assert other.json()["code"] == "ACCOUNT_NOT_FOUND"
    # 本人访问自己的账户仍然成功——对照组排除"整条路径坏了"的可能。
    assert client.get(
        "/v1/ledger/summary", headers=_auth(issued_b), params={"scope": ACCOUNT_B}
    ).status_code == 200


# --------------------------------------------------------------------------
# V04：同文件分别导入，作业与令牌互不可用
# --------------------------------------------------------------------------


def test_the_same_file_is_imported_independently_by_both_users(wired) -> None:
    """A/B 上传**同一个文件**：各自绑定自己的账户、各自入账、互不去重。

    ``transactions`` 的主键是 ``(account_id, transaction_hash)``，因此同一份文件在两
    个账户下必然产生两行——若去重或绑定带了全库范围，其中一方会拿到 0 插入或被绑到
    对方的账户上。
    """
    client, issued_a, issued_b, _, _ = wired
    content = _csv("01/05/2026", "-$400.00")

    preview_a = _preview(client, issued_a, content)
    preview_b = _preview(client, issued_b, content)
    assert preview_a.status_code == 201 and preview_b.status_code == 201
    assert preview_a.json()["preview"]["accountId"] == ACCOUNT_A
    assert preview_b.json()["preview"]["accountId"] == ACCOUNT_B
    # 各自的基准都是 0：B 的预览不受 A 的账本状态影响。
    assert preview_a.json()["preview"]["baseLedgerVersion"] == 0
    assert preview_b.json()["preview"]["baseLedgerVersion"] == 0
    # 两边都认为这一行可以插入，说明去重是按账户做的。
    assert preview_a.json()["preview"]["insertableRows"] == 1
    assert preview_b.json()["preview"]["insertableRows"] == 1

    commit_a = _commit(client, issued_a, preview_a.json()["importId"],
                       preview_a.json()["confirmToken"])
    commit_b = _commit(client, issued_b, preview_b.json()["importId"],
                       preview_b.json()["confirmToken"])

    assert commit_a.status_code == 200, commit_a.text
    assert commit_b.status_code == 200, commit_b.text
    assert commit_a.json()["insertedRows"] == 1
    assert commit_b.json()["insertedRows"] == 1
    assert _transaction_count(client, issued_a, ACCOUNT_A) == 1
    assert _transaction_count(client, issued_b, ACCOUNT_B) == 1


def test_another_users_import_job_cannot_be_read_or_cancelled(wired) -> None:
    """B 读或取消 A 的作业 → **与作业不存在完全相同的响应**。"""
    client, issued_a, issued_b, _, _ = wired

    job_id = _preview(client, issued_a, _csv("01/05/2026", "-$400.00")).json()["importId"]

    stolen_read = client.get(f"/v1/imports/{job_id}", headers=_auth(issued_b))
    missing_read = client.get(f"/v1/imports/{UNKNOWN_JOB}", headers=_auth(issued_b))
    assert stolen_read.status_code == 409
    assert _error_payload(stolen_read) == _error_payload(missing_read)

    stolen_cancel = client.post(f"/v1/imports/{job_id}/cancel", headers=_auth(issued_b))
    missing_cancel = client.post(f"/v1/imports/{UNKNOWN_JOB}/cancel", headers=_auth(issued_b))
    assert stolen_cancel.status_code == 409
    assert _error_payload(stolen_cancel) == _error_payload(missing_cancel)

    # A 的作业没被 B 动过，仍是可提交的预览。
    still_mine = client.get(f"/v1/imports/{job_id}", headers=_auth(issued_a))
    assert still_mine.status_code == 200
    assert still_mine.json()["status"] == "preview_ready"


def test_another_users_preview_token_cannot_commit(wired) -> None:
    """B 拿到 A 的作业 ID 与明文令牌也不能提交——授权先看归属，再看令牌。"""
    client, issued_a, issued_b, _, _ = wired
    preview = _preview(client, issued_a, _csv("01/05/2026", "-$400.00")).json()

    stolen = _commit(client, issued_b, preview["importId"], preview["confirmToken"])
    assert stolen.status_code == 409

    # A 用同一个令牌仍能正常提交：B 的尝试没有消费掉它，也没写任何账本。
    assert _commit(client, issued_a, preview["importId"], preview["confirmToken"]).status_code == 200
    assert _transaction_count(client, issued_a, ACCOUNT_A) == 1
    assert _transaction_count(client, issued_b, ACCOUNT_B) == 0


# --------------------------------------------------------------------------
# V05：版本按用户，B 的写入不让 A 的预览失效
# --------------------------------------------------------------------------


def test_another_users_write_does_not_invalidate_a_preview(wired) -> None:
    """**A 生成预览后 B 提交：B 的写入不改变 A 的基准版本。**

    判据是「用户当前账本版本」= ``max(version) WHERE user_id = ?``（契约 §3.3）。
    用全局 ``max(version)`` 时，B 的提交会把 A 的当前版本抬高，A 随后提交自己的预览
    会被判 ``LEDGER_VERSION_CONFLICT``——用户遇到的是"我没做任何事，预览却过期了"。
    """
    client, issued_a, issued_b, _, _ = wired

    # A 先预览（基准 0），但先不提交。
    preview_a = _preview(client, issued_a, _csv("01/05/2026", "-$400.00")).json()
    assert preview_a["preview"]["baseLedgerVersion"] == 0

    # B 完整导入一份文件，推进的是 B 自己的版本。
    preview_b = _preview(client, issued_b, _csv("01/06/2026", "-$500.00", "$50.00")).json()
    assert _commit(client, issued_b, preview_b["importId"], preview_b["confirmToken"]).status_code == 200

    # **A 的版本仍是 0**：B 的写入不越界。
    assert _summary(client, issued_a, ACCOUNT_A)["metadata"]["ledgerVersion"] == 0

    # A 的预览仍然有效，正常提交。
    commit_a = _commit(client, issued_a, preview_a["importId"], preview_a["confirmToken"])
    assert commit_a.status_code == 200, commit_a.text
    assert _transaction_count(client, issued_a, ACCOUNT_A) == 1


def test_a_users_own_stale_preview_is_still_detected(wired) -> None:
    """**A 自己的版本冲突仍按既有规则检测**，不能因为改成按用户取就漏判。

    A 先生成预览二（基准 v），随后 A 自己又导入文件三（版本 → v+1）；此时提交预览二
    必须报 ``LEDGER_VERSION_CONFLICT``，提示重新预览。
    """
    client, issued_a, _, _, _ = wired

    stale = _preview(client, issued_a, _csv("01/06/2026", "-$500.00", "$50.00")).json()

    # A 自己推进版本：另一次完整导入。
    fresh = _preview(client, issued_a, _csv("01/07/2026", "-$600.00", "$60.00")).json()
    assert _commit(client, issued_a, fresh["importId"], fresh["confirmToken"]).status_code == 200

    conflict = _commit(client, issued_a, stale["importId"], stale["confirmToken"])

    assert conflict.status_code == 409
    assert conflict.json()["code"] == "LEDGER_VERSION_CONFLICT"
    assert conflict.json()["details"] == {
        "expected_ledger_version": 0,
        "actual_ledger_version": 1,
    }
