"""导入作业路由测试。

覆盖认证、读取与取消三条路径，以及一条安全选择：**作业不存在时返回 409 而不是
404**——否则未授权方可以靠状态码探测某个作业 ID 是否存在。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.protocol import CLIENT_PROTOCOL_HEADERS
from lyra.application.device_pairing_service import DevicePairingService
from lyra.application.import_job import PersistedImportJobRecord
from lyra.application.write_coordinator import WriteCoordinator
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.infrastructure.db.import_job_repository import DuckDbImportJobRepository
from lyra.main import create_app

PUBLIC_KEY_PEM = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)["cases"][0]["inputPem"]
IDENTITY = LoginIdentity(provider="github", subject="12345", email=None, display_name="octocat")
JOB_ID = "44444444-4444-4444-4444-444444444444"
ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"


def _job(status: str = "preview_ready") -> PersistedImportJobRecord:
    """构造一条作业记录。"""
    return PersistedImportJobRecord(
        id=JOB_ID, file_hash="a" * 64, original_file_name="ledger.csv", file_type="csv",
        file_size_bytes=2048, parser_version="2", status=status,  # type: ignore[arg-type]
        target_account_id=None, preview_token_hash="b" * 64, preview_ledger_version=None,
        total_rows=10, inserted_rows=8, skipped_rows=2, error_code=None,
        error_summary=None, committed_ledger_version=None,
        created_at="2026-10-12T08:00:00.000Z", updated_at="2026-10-12T08:30:00.000Z",
    )


@pytest.fixture()
def client(tmp_path: Path):
    """构造已认证的应用与一个有效访问令牌，并预置一个作业。"""
    database_path = tmp_path / "imports.duckdb"
    database = open_lyra_database(str(database_path))
    # 预览需要把文件里的账户标识绑定到账户，因此夹具必须有一个账户。
    database.connection.execute(
        """
        INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), 'Account', 'TST001', '001')
        """,
        [ACCOUNT_ID],
    )
    DuckDbImportJobRepository(database.connection).create_import_job(_job())

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


def _auth(issued) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {issued.access_token}",
        # **每个 /v1 请求都必须声明客户端协议版本**（除公开路由）。
        # 缺失时服务端返回 426——那是客户端唯一能据此升级 App 的信号。
        "x-lyra-protocol-version": "2",
    }


# --------------------------------------------------------------------------
# 认证
# --------------------------------------------------------------------------


def test_import_routes_require_authentication(client) -> None:
    """两条路由都必须要求设备认证。"""
    test_client, _ = client
    assert test_client.get(f"/v1/imports/{JOB_ID}").status_code == 401
    assert test_client.post(f"/v1/imports/{JOB_ID}/cancel").status_code == 401


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------


def test_read_job_returns_public_state(client) -> None:
    """读取返回公开作业状态，含恢复流程所需字段。"""
    test_client, issued = client
    response = test_client.get(f"/v1/imports/{JOB_ID}", headers=_auth(issued))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == JOB_ID
    assert body["fileName"] == "ledger.csv"
    assert body["status"] == "preview_ready"
    assert body["totalRows"] == 10
    assert body["insertedRows"] == 8
    assert body["skippedRows"] == 2
    assert body["baseLedgerVersion"] is None


def test_read_job_never_leaks_commit_secrets(client) -> None:
    """公开作业不得带文件哈希、解析器版本或预览令牌摘要。"""
    test_client, issued = client
    body = test_client.get(f"/v1/imports/{JOB_ID}", headers=_auth(issued)).json()

    for hidden in ("fileHash", "parserVersion", "previewTokenHash", "errorSummary"):
        assert hidden not in body, f"公开作业泄漏了 {hidden}"


def test_read_unknown_job_is_conflict_not_not_found(client) -> None:
    """未知作业返回 409 而不是 404，避免探测作业是否存在。"""
    test_client, issued = client
    response = test_client.get(
        "/v1/imports/99999999-9999-9999-9999-999999999999", headers=_auth(issued)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "IMPORT_STATE_CONFLICT"


# --------------------------------------------------------------------------
# 取消
# --------------------------------------------------------------------------


def test_cancel_transitions_to_cancelled(client) -> None:
    """取消把作业推进到 cancelled。"""
    test_client, issued = client
    response = test_client.post(f"/v1/imports/{JOB_ID}/cancel", headers=_auth(issued))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    # 再读一次确认已落库。
    assert test_client.get(
        f"/v1/imports/{JOB_ID}", headers=_auth(issued)
    ).json()["status"] == "cancelled"


def test_cancel_is_idempotent_over_http(client) -> None:
    """重复取消返回同一结果，而不是报冲突。"""
    test_client, issued = client
    first = test_client.post(f"/v1/imports/{JOB_ID}/cancel", headers=_auth(issued))
    second = test_client.post(f"/v1/imports/{JOB_ID}/cancel", headers=_auth(issued))

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "cancelled"


def test_cancel_unknown_job_is_conflict(client) -> None:
    """未知作业取消返回 409。"""
    test_client, issued = client
    response = test_client.post(
        "/v1/imports/99999999-9999-9999-9999-999999999999/cancel", headers=_auth(issued)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "IMPORT_STATE_CONFLICT"


def test_cancel_rejected_while_committing(client) -> None:
    """提交中的作业不可取消。"""
    test_client, issued = client
    connection = test_client.app.state.database.connection
    connection.execute(
        "UPDATE import_files SET status = 'committing' WHERE id = CAST(? AS UUID)",
        [JOB_ID],
    )

    response = test_client.post(f"/v1/imports/{JOB_ID}/cancel", headers=_auth(issued))

    assert response.status_code == 409
    assert response.json()["code"] == "IMPORT_STATE_CONFLICT"


def test_import_routes_still_report_ledger_routes(client) -> None:
    """挂载导入路由不影响既有账本路由。"""
    test_client, issued = client
    assert test_client.get("/v1/ledger/summary", headers=_auth(issued), params={"scope": "all"}).status_code == 200
    assert test_client.get("/v1/imports/" + JOB_ID, headers=_auth(issued)).status_code == 200


def test_import_routes_do_not_leak_snake_case_keys(client) -> None:
    """导入路由的公开响应里也不得残留 snake_case 字段名。

    通用序列化器按 dataclass 字段名出站，会把内部命名直接发给客户端；客户端只会
    读到 ``undefined``，不会报错。这条断言与账本路由的那条互补，覆盖新增端点。
    """
    test_client, issued = client

    def snake_keys(value: object, prefix: str = "") -> list[str]:
        found: list[str] = []
        if isinstance(value, dict):
            for key, child in value.items():
                if "_" in key:
                    found.append(f"{prefix}{key}")
                found.extend(snake_keys(child, f"{prefix}{key}."))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                found.extend(snake_keys(child, f"{prefix}[{index}]."))
        return found

    for response in (
        test_client.get(f"/v1/imports/{JOB_ID}", headers=_auth(issued)),
        test_client.post(f"/v1/imports/{JOB_ID}/cancel", headers=_auth(issued)),
    ):
        leaked = snake_keys(response.json())
        assert not leaked, f"导入响应泄漏 snake_case 字段: {leaked}"


# --------------------------------------------------------------------------
# 预览
# --------------------------------------------------------------------------

CSV_HEADER = "Date,Action,Symbol,Description,Quantity,Price,Fees & Comm,Amount\n"
CSV_ROW = '"01/05/2026","Buy","INTC","INTEL CORP","10","$40.00","","-$400.00"\n'
# 账户标识由文件名解析：Individual_<account>_...
CSV_NAME = "Individual_TST001_Transactions_20260105.csv"


def _preview(client, content: bytes = (CSV_HEADER + CSV_ROW).encode(), name: str = CSV_NAME, **query):
    test_client, issued = client
    params = "&".join(f"{key}={value}" for key, value in query.items())
    return test_client.post(
        f"/v1/imports/preview?{params}" if params else "/v1/imports/preview",
        content=content,
        headers={**_auth(issued), "x-lyra-file-name": name, "content-type": "application/octet-stream"},
    )


def test_preview_requires_authentication(client) -> None:
    """预览端点必须要求设备认证。"""
    test_client, _ = client
    assert test_client.post("/v1/imports/preview", content=b"x").status_code == 401


def test_preview_requires_file_name_header(client) -> None:
    """缺少 x-lyra-file-name 头必须 400。"""
    test_client, issued = client
    response = test_client.post(
        "/v1/imports/preview", content=b"x", headers=_auth(issued)
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


def test_preview_accepts_upload_and_returns_token(client, tmp_path) -> None:
    """合法上传返回预览与一次性令牌，并落一个 preview_ready 作业。"""
    connection = None
    test_client, issued = client
    response = _preview(client)

    # **201**（与参考实现一致）——创建了一个新作业，而不是返回已有资源。
    assert response.status_code == 201
    body = response.json()
    assert body["importId"]
    assert body["accountId"] == ACCOUNT_ID
    assert body["fileName"] == CSV_NAME
    assert body["totalRows"] == 1
    assert body["insertableRows"] == 1
    assert body["existingRows"] == 0
    assert body["baseLedgerVersion"] == 0
    assert body["byAction"] == {"Buy": 1}
    assert body["firstTransactionDate"] == "2026-01-05"
    assert body["previewToken"]

    # 作业必须已经进入 preview_ready，且只存令牌哈希。
    connection = test_client.app.state.database.connection
    row = connection.execute(
        "SELECT status, preview_token_hash, preview_ledger_version FROM import_files WHERE id = CAST(? AS UUID)",
        [body["importId"]],
    ).fetchone()
    assert row is not None
    assert row[0] == "preview_ready"
    assert row[1] != body["previewToken"], "数据库里存了明文令牌"
    assert len(row[1]) == 64
    # 空账本用 NULL 表示基准 0：该列引用 ledger_versions，没有 version 0 的行。
    assert row[2] is None


def test_preview_never_leaks_the_fingerprint(client) -> None:
    """预览指纹是服务端内部凭据，不得外发。"""
    body = _preview(client).json()
    assert "previewFingerprint" not in body
    assert "preview_fingerprint" not in body


def test_preview_has_no_snake_case_keys(client) -> None:
    """预览响应同样不得出现 snake_case 字段。"""
    body = _preview(client).json()
    for key in body:
        assert "_" not in key, f"预览响应泄漏 snake_case 字段: {key}"


def test_preview_requires_account_mapping_when_unknown(client) -> None:
    """文件账户标识对不上任何账户时要求显式选择。"""
    response = _preview(client, name="Individual_NOPE_Transactions.csv")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "ACCOUNT_MAPPING_REQUIRED"
    assert body["details"] == {"candidate_account_ids": []}


def test_preview_accepts_a_real_xlsx(client) -> None:
    """**xlsx 上传现在走真实解析**（此前返回 501 的缺口已闭合）。

    端到端验证：上传 → 预览成功，且笔数与 CSV 路径一致。
    """
    response = _preview(
        client, content=_xlsx_content(), name="Individual_TST001_Transactions.xlsx"
    )

    # **201**（与参考实现一致）。
    assert response.status_code == 201
    assert response.json()["totalRows"] == 1


def test_preview_reports_a_corrupt_xlsx_as_validation_failure(client) -> None:
    """损坏的 xlsx 报 422（内容不可读），而不是 501（格式未实现）。

    两者对用户的下一步动作完全不同：前者要检查文件，后者要换格式。
    """
    response = _preview(
        client, content=b"\x50\x4b\x03\x04" + b"\x00" * 32,
        name="Individual_TST001_Transactions.xlsx",
    )

    assert response.status_code in (422, 400)
    assert response.json()["code"] != "IMPORT_FORMAT_NOT_IMPLEMENTED"


def _xlsx_content() -> bytes:
    """生成一份含一行交易的真实工作簿。"""
    import io

    import openpyxl

    from lyra.infrastructure.imports.schwab_table import SCHWAB_TRANSACTION_HEADERS

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Transactions"
    sheet.append(list(SCHWAB_TRANSACTION_HEADERS))
    sheet.append(["01/05/2026", "Buy", "INTC", "INTEL CORP", "10", "$40.5", "", "-$405"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_preview_reports_bad_headers_as_validation_failure(client) -> None:
    """表头不符返回 422 而不是 500。"""
    response = _preview(client, content=b"Foo,Bar\n1,2\n")

    assert response.status_code == 422
    assert response.json()["code"] == "IMPORT_VALIDATION_FAILED"


def test_preview_rejects_unsupported_extension(client) -> None:
    """非 csv/xlsx 返回 415。"""
    response = _preview(client, name="Individual_TST001_ledger.pdf")
    assert response.status_code == 415
    assert response.json()["code"] == "IMPORT_UNSUPPORTED_FORMAT"


# --------------------------------------------------------------------------
# 提交
# --------------------------------------------------------------------------


def _commit(client, import_id: str, token: str, extra_headers: dict | None = None):
    """提交一个导入；``extra_headers`` 用于附加协议头（如幂等键）。"""
    test_client, issued = client
    return test_client.post(
        f"/v1/imports/{import_id}/commit",
        json={"confirmToken": token},
        headers={**_auth(issued), **(extra_headers or {})},
    )


def _flow(client, *, name: str = CSV_NAME):
    """走一遍预览，返回 (importId, token)。"""
    body = _preview(client, name=name).json()
    return body["importId"], body["previewToken"]


def test_commit_requires_authentication(client) -> None:
    """提交端点必须要求设备认证。"""
    test_client, _ = client
    assert test_client.post("/v1/imports/x/commit", json={"confirmToken": "t"}).status_code == 401


def test_commit_rejects_invalid_body(client) -> None:
    """缺少 confirmToken 必须 400。"""
    test_client, issued = client
    for payload in ({}, {"confirmToken": ""}, {"confirmToken": 123}):
        response = test_client.post(
            "/v1/imports/44444444-4444-4444-4444-444444444444/commit",
            json=payload,
            headers=_auth(issued),
        )
        assert response.status_code == 400, payload
        assert response.json()["code"] == "INVALID_ARGUMENT"


def test_commit_end_to_end_writes_the_ledger(client) -> None:
    """完整流程：预览 → 提交 → 账本落库 → 作业终态 → 暂存清理。"""
    test_client, issued = client
    import_id, token = _flow(client)
    connection = test_client.app.state.database.connection

    response = _commit(client, import_id, token)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "changed": True,
        "ledgerVersion": 1,
        "insertedRows": 1,
        "skippedRows": 0,
    }
    # 账本真的写了。
    assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM lots").fetchone()[0] == 1
    # 作业进入终态。
    status = connection.execute(
        "SELECT status, committed_ledger_version FROM import_files WHERE id = CAST(? AS UUID)",
        [import_id],
    ).fetchone()
    assert status is not None
    assert status[0] in {"committed", "committing"}
    # 暂存必须清理，不把账本数据留在磁盘上。
    staging = Path(test_client.app.state.settings.lyra_import_staging_dir)
    assert list(staging.iterdir()) == []

    # 提交后读作业应看到 committed。
    job = test_client.get(f"/v1/imports/{import_id}", headers=_auth(issued)).json()
    assert job["status"] == "committed"


def test_commit_rejects_wrong_token(client) -> None:
    """错误的确认令牌必须被拒，且不写账本。"""
    test_client, _ = client
    import_id, _ = _flow(client)

    response = _commit(client, import_id, "wrong-token")

    assert response.status_code == 409
    assert response.json()["code"] == "IMPORT_PREVIEW_STALE"
    assert test_client.app.state.database.connection.execute(
        "SELECT count(*) FROM transactions"
    ).fetchone()[0] == 0


def test_commit_twice_is_rejected(client) -> None:
    """同一令牌不能提交两次。"""
    test_client, _ = client
    import_id, token = _flow(client)
    assert _commit(client, import_id, token).status_code == 200

    second = _commit(client, import_id, token)

    assert second.status_code == 409
    assert test_client.app.state.database.connection.execute(
        "SELECT count(*) FROM transactions"
    ).fetchone()[0] == 1


def test_commit_without_staged_upload_is_rejected(client) -> None:
    """暂存文件缺失（已被清理或从未落盘）时不得提交。"""
    test_client, _ = client
    import_id, token = _flow(client)
    staging = Path(test_client.app.state.settings.lyra_import_staging_dir)
    for entry in staging.iterdir():
        entry.unlink()

    response = _commit(client, import_id, token)

    assert response.status_code == 409
    assert response.json()["code"] == "IMPORT_PREVIEW_STALE"


def test_commit_unknown_job_is_conflict(client) -> None:
    """未知作业提交返回 409。"""
    response = _commit(client, "99999999-9999-9999-9999-999999999999", "t")
    assert response.status_code == 409
    assert response.json()["code"] == "IMPORT_STATE_CONFLICT"


# --------------------------------------------------------------------------
# 幂等键：当前缺口（固定下来，接线后必须更新）
# --------------------------------------------------------------------------


def test_idempotency_key_does_not_break_a_missing_job(client) -> None:
    """带幂等键请求一个不存在的作业时，仍然是 409 而不是 400/500。

    **这条测试在幂等接线前后的含义不同**，值得说明：

    - 接线前它固定的是"缺口：头被忽略但至少不被拒"（§144.3）；
    - 接线后它固定的是"**规划阶段的失败不会掩盖作业本身的问题**"——未知作业仍然
      如实报 409，而不是因为先处理了幂等键就变成一个别的错误。

    真正验证"键被采用"的是下面那组端到端测试（重复提交返回相同响应且不二次写入）。
    """
    response = _commit(
        client, import_id="44444444-4444-4444-4444-444444444444",
        token="token", extra_headers={"Idempotency-Key": "key-000000000001"},
    )

    # 未知作业 → 409；关键是**不是 400**（说明头本身没被拒绝）。
    assert response.status_code == 409


def test_commit_without_an_idempotency_key_still_works(client) -> None:
    """不带该头时行为不变——接入后也必须保持这一点。"""
    response = _commit(
        client, import_id="44444444-4444-4444-4444-444444444444", token="token"
    )

    assert response.status_code == 409


# --------------------------------------------------------------------------
# 幂等键：端到端
# --------------------------------------------------------------------------


def _commit_with_key(client, import_id: str, token: str, key: str):
    """带幂等键提交。"""
    return _commit(client, import_id=import_id, token=token,
                   extra_headers={"Idempotency-Key": key})


def test_committing_with_an_idempotency_key_succeeds(client) -> None:
    """带键提交正常成功，并返回作业 ID。"""
    import_id, token = _flow(client)

    response = _commit_with_key(client, import_id, token, "key-000000000001")

    assert response.status_code == 200
    # **提交返回的是提交结果，不是作业载荷**（与参考实现一致）。
    assert set(response.json()) == {"changed", "ledgerVersion", "insertedRows", "skippedRows"}


def test_replaying_the_same_key_returns_the_same_job_without_a_second_write(client) -> None:
    """**重复提交返回同一个作业，且不产生第二次写入。**

    这正是幂等键要保证的事。第二次请求走的是 ``replay`` 路径——它**不执行任何写入**，
    只把首次的结果投影回来。

    若第二次真的又提交了一次，账本版本会再涨一级（而交易因 ``transaction_hash`` 去重
    不会翻倍）——**用户看到的是"版本涨了但笔数没变"**，一个难以解释的状态。
    """
    import_id, token = _flow(client)
    first = _commit_with_key(client, import_id, token, "key-replay-000001")
    assert first.status_code == 200
    ledger_version_after_first = first.json()["ledgerVersion"]

    second = _commit_with_key(client, import_id, token, "key-replay-000001")

    assert second.status_code == 200
    # **回放返回与首次相同的形状与数值。**
    assert second.json() == first.json()
    # **账本版本没有再次推进**——证明确实没有再写一次。
    assert second.json()["ledgerVersion"] == ledger_version_after_first


def test_replaying_without_a_key_does_not_replay(client) -> None:
    """**不带键时不走回放**：客户端没要求幂等，服务端不代它登记。

    第二次提交会因作业状态已离开 ``preview_ready`` 而冲突——这本身也是保护。
    """
    import_id, token = _flow(client)
    first = _commit(client, import_id=import_id, token=token)
    assert first.status_code == 200

    second = _commit(client, import_id=import_id, token=token)

    assert second.status_code == 409


def test_the_same_key_on_a_different_request_is_reused(client) -> None:
    """**同一个键配不同的请求指纹 → 409 ``IDEMPOTENCY_KEY_REUSED``。**

    否则客户端用一个键提交两份不同的文件时，第二次会被"回放"成第一次的结果，
    而用户以为第二份文件已经导入。
    """
    first_id, first_token = _flow(client)
    _commit_with_key(client, first_id, first_token, "shared-key-000001")

    # 同一台设备、同一个键，但另一个作业（即另一个请求指纹）。
    second_id, second_token = _flow(client, name="Individual_TST001_Transactions2.csv")
    response = _commit_with_key(client, second_id, second_token, "shared-key-000001")

    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_different_keys_do_not_interfere(client) -> None:
    """不同键互不干扰。"""
    import_id, token = _flow(client)
    first = _commit_with_key(client, import_id, token, "key-aaaaaaaaaaaa01")
    assert first.status_code == 200

    other_id, other_token = _flow(client, name="Individual_TST001_Transactions2.csv")
    second = _commit_with_key(client, other_id, other_token, "key-bbbbbbbbbbbb01")

    assert second.status_code == 200
    assert second.json()["insertedRows"] >= 0


# --------------------------------------------------------------------------
# 请求侧契约：路径 ID、令牌上限、幂等键格式
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v1/imports/not-a-uuid",
        "/v1/imports/not-a-uuid/cancel",
        "/v1/imports/not-a-uuid/commit",
    ],
)
def test_a_malformed_import_id_is_a_400_not_a_404(client, path: str) -> None:
    """**格式非法的作业 ID 返回 400，而不是 404。**

    400 说"你的请求构造错了"，404 说"这个作业不存在"——客户端对两者的处理完全不同
    （改代码 vs 刷新列表）。三条按 ID 操作的路由都要这样，因此参数化覆盖。
    """
    test_client, issued = client
    # 读取用 GET，另两条用 POST。
    response = (
        test_client.post(path, headers=_auth(issued), json={"confirmToken": "t"})
        if path.endswith(("/cancel", "/commit"))
        else test_client.get(path, headers=_auth(issued))
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


def test_a_confirm_token_longer_than_the_limit_is_rejected(client) -> None:
    """**确认令牌上限 512**（与参考实现的 schema 一致）。

    没有上限时，超长令牌会一路进入哈希与比较，而客户端的截断逻辑按那个上限设计。
    """
    import_id, _ = _flow(client)
    response = _commit(client, import_id=import_id, token="x" * 513)

    assert response.status_code == 400


def test_an_idempotency_key_shorter_than_the_minimum_is_rejected(client) -> None:
    """**幂等键至少 16 位安全字符**（参考实现：`^[A-Za-z0-9._:-]{16,200}$`）。

    不检查会让客户端以为短键可用——**而切换后同样的键会被拒**，那时才发现。
    """
    import_id, token = _flow(client)
    response = _commit_with_key(client, import_id, token, "short")

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


def test_an_idempotency_key_with_unsafe_characters_is_rejected(client) -> None:
    """幂等键只允许安全字符——**键会进入日志与数据库**，任意字符会带来注入面。"""
    import_id, token = _flow(client)
    response = _commit_with_key(client, import_id, token, "key with spaces!!")

    assert response.status_code == 400


def test_an_idempotency_key_at_the_minimum_length_is_accepted(client) -> None:
    """**恰好 16 位是合法的**——下限是闭区间。

    边界值值得单独断言：写成 `>` 会拒掉合法的键，而这类差一位的错误在只有"太短"的
    测试里永远不会暴露。
    """
    import_id, token = _flow(client)
    response = _commit_with_key(client, import_id, token, "a" * 16)

    assert response.status_code == 200
