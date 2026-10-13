"""导入作业仓储的集成测试（真实 DuckDB）。

重点是 **CAS（比较并交换）状态转换**：领域层的转换判定只在内存里做，而两个并发
请求可能读到同一个旧状态、各自判定合法、然后都去写库。用数据库的唯一裁决
（``WHERE id = ? AND status = ?``）保证只有一方成功。

这类 bug 在单进程测试里不会出现，但必须把"第二个请求会失败"这条契约钉住，
否则将来有人把它改成无条件 UPDATE，测试也不会红。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.import_job import PersistedImportJobRecord
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.db.import_job_repository import DuckDbImportJobRepository

ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"
JOB_ID = "44444444-4444-4444-4444-444444444444"
NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)


def _record(**overrides) -> PersistedImportJobRecord:
    """构造一条 ``uploaded`` 状态的作业记录。"""
    base = {
        "id": JOB_ID,
        "file_hash": hashlib.sha256(b"file").hexdigest(),
        "original_file_name": "ledger.csv",
        "file_type": "csv",
        "file_size_bytes": 2048,
        "parser_version": "2",
        "status": "uploaded",
        "target_account_id": None,
        "preview_token_hash": None,
        "preview_ledger_version": None,
        "total_rows": None,
        "inserted_rows": None,
        "skipped_rows": None,
        "error_code": None,
        "error_summary": None,
        "committed_ledger_version": None,
        "created_at": "2026-10-12T08:00:00.000Z",
        "updated_at": "2026-10-12T08:00:00.000Z",
    }
    base.update(overrides)
    return PersistedImportJobRecord(**base)  # type: ignore[arg-type]


@pytest.fixture()
def wired(tmp_path: Path):
    """在临时真实库上装配仓储与账户。"""
    database = open_lyra_database(str(tmp_path / "jobs.duckdb"))
    connection = database.connection
    connection.execute(
        """
        INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), 'Account', 'TST001', '001')
        """,
        [ACCOUNT_ID],
    )
    # import_files.preview_ledger_version 引用 ledger_versions，先让序列分配出版本号。
    for _ in range(3):
        connection.execute(
            "INSERT INTO ledger_versions (reason, source_reference) VALUES ('file_import', 'seed')"
        )
    try:
        yield DuckDbImportJobRepository(connection), connection
    finally:
        database.close()


def test_create_and_load_round_trip(wired) -> None:
    """创建后读回必须逐字段一致，且时间戳为 ISO 文本。"""
    repository, _ = wired
    repository.create_import_job(_record())

    loaded = repository.load_import_job(JOB_ID)

    assert loaded is not None
    assert loaded.file_hash == _record().file_hash
    assert loaded.status == "uploaded"
    assert loaded.target_account_id is None
    assert loaded.total_rows is None
    assert isinstance(loaded.created_at, str)
    assert loaded.created_at == "2026-10-12T08:00:00.000Z"


def test_load_unknown_job_returns_none(wired) -> None:
    """不存在的作业返回 None，而不是抛错。"""
    repository, _ = wired
    assert repository.load_import_job("99999999-9999-9999-9999-999999999999") is None


# --------------------------------------------------------------------------
# CAS 状态转换
# --------------------------------------------------------------------------


def test_transition_succeeds_when_status_matches(wired) -> None:
    """状态相符时推进成功并刷新更新时间。"""
    repository, _ = wired
    repository.create_import_job(_record())

    assert repository.transition_status(JOB_ID, "uploaded", "parsing", NOW) is True

    loaded = repository.load_import_job(JOB_ID)
    assert loaded is not None
    assert loaded.status == "parsing"
    assert loaded.updated_at == "2026-10-12T09:00:00.000Z"


def test_transition_fails_when_status_already_changed(wired) -> None:
    """**第二个并发请求必须失败**，而不是覆盖第一个的结果。"""
    repository, _ = wired
    repository.create_import_job(_record())
    # 第一个请求胜出。
    assert repository.transition_status(JOB_ID, "uploaded", "parsing", NOW) is True

    # 第二个请求仍拿着旧状态。
    assert repository.transition_status(JOB_ID, "uploaded", "cancelled", NOW) is False

    loaded = repository.load_import_job(JOB_ID)
    assert loaded is not None
    assert loaded.status == "parsing", "CAS 失败却改写了状态"


def test_transition_on_terminal_state_is_rejected(wired) -> None:
    """终态作业不能出边——仓储层也必须拦住，而不是只靠调用方先校验。

    若仓储只做 CAS，调用方一旦忘记先调 :func:`transition_import_job`，就能把
    「已提交」写成「提交中」，而账本已经写过了。
    """
    repository, _ = wired
    repository.create_import_job(_record(status="committed"))

    with pytest.raises(ApplicationError) as raised:
        repository.transition_status(JOB_ID, "committed", "committing", NOW)

    assert raised.value.code == "IMPORT_STATE_CONFLICT"
    loaded = repository.load_import_job(JOB_ID)
    assert loaded is not None and loaded.status == "committed"


def test_transition_unknown_job_returns_false(wired) -> None:
    """不存在的作业转换返回 False（0 行受影响）。"""
    repository, _ = wired
    assert repository.transition_status(
        "99999999-9999-9999-9999-999999999999", "uploaded", "parsing", NOW
    ) is False


# --------------------------------------------------------------------------
# 预览写入
# --------------------------------------------------------------------------


def test_record_preview_writes_metadata_and_advances_status(wired) -> None:
    """解析完成后一次性写入预览元数据并把状态推进到 preview_ready。"""
    repository, _ = wired
    repository.create_import_job(_record(status="parsing"))

    assert repository.record_preview(
        JOB_ID,
        target_account_id=ACCOUNT_ID,
        preview_token_hash="c" * 64,
        preview_fingerprint="d" * 64,
        preview_expires_at=NOW,
        preview_ledger_version=3,
        total_rows=10,
        inserted_rows=8,
        skipped_rows=2,
        now=NOW,
    ) is True

    loaded = repository.load_import_job(JOB_ID)
    assert loaded is not None
    assert loaded.status == "preview_ready"
    assert loaded.target_account_id == ACCOUNT_ID
    assert loaded.preview_ledger_version == 3
    assert loaded.inserted_rows == 8
    assert loaded.skipped_rows == 2


def test_record_preview_is_rejected_unless_parsing(wired) -> None:
    """只有仍在解析中的作业才能写入预览结果（同样是 CAS）。"""
    repository, _ = wired
    repository.create_import_job(_record(status="uploaded"))

    assert repository.record_preview(
        JOB_ID,
        target_account_id=ACCOUNT_ID,
        preview_token_hash="c" * 64,
        preview_fingerprint="d" * 64,
        preview_expires_at=NOW,
        preview_ledger_version=1,
        total_rows=1,
        inserted_rows=1,
        skipped_rows=0,
        now=NOW,
    ) is False

    loaded = repository.load_import_job(JOB_ID)
    assert loaded is not None
    assert loaded.status == "uploaded"
    assert loaded.total_rows is None


def test_preview_ledger_version_must_exist(wired) -> None:
    """预览基准版本引用 ledger_versions，凭空写一个值会被外键拒绝。"""
    repository, _ = wired
    repository.create_import_job(_record(status="parsing"))

    with pytest.raises(Exception, match="foreign key|Constraint"):
        repository.record_preview(
            JOB_ID,
            target_account_id=ACCOUNT_ID,
            preview_token_hash="c" * 64,
            preview_fingerprint="d" * 64,
            preview_expires_at=NOW,
            preview_ledger_version=99,
            total_rows=1,
            inserted_rows=1,
            skipped_rows=0,
            now=NOW,
        )
