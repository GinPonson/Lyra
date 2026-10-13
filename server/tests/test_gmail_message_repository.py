"""Gmail 消息归档仓储测试。

两条行为值得固定下来：

1. ``content_hash`` 为 NULL 的行视为"没有已处理状态"；
2. 消息行是**普通 INSERT**，主键 ``(connection_id, message_id)``，因此重复归档会
   撞主键。这条测试把真实行为写下来，避免以后无意中依赖某个未言明的调用顺序。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from lyra.db.connection import open_lyra_database
from lyra.domain.gmail.econfirm import GmailMessage
from lyra.infrastructure.db.gmail_message_repository import (
    load_existing_gmail_message_state,
    persist_unsuccessful_gmail_message,
)
from lyra.infrastructure.gmail.message_metadata import (
    create_unsuccessful_gmail_message_metadata,
)

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
SUFFIX = "276"


@pytest.fixture()
def fixture(tmp_path: Path):
    """建立连接、账户与一条消息。"""
    handle = open_lyra_database(str(tmp_path / "msg.duckdb"))
    account_id = str(uuid.uuid4())
    handle.connection.execute(
        """
        INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), 'Account', 'TST001', ?)
        """,
        [account_id, SUFFIX],
    )
    connection_id = str(uuid.uuid4())
    handle.connection.execute(
        """
        INSERT INTO gmail_connections (
            id, google_subject, google_email, encrypted_refresh_token,
            granted_scopes, status
        ) VALUES (CAST(? AS UUID), 's1', 'a@b.com', 'v1.x.y.z', 'scope', 'pending_initial_scan')
        """,
        [connection_id],
    )
    try:
        yield handle, connection_id, account_id
    finally:
        handle.close()


def _message(body: str = "b") -> GmailMessage:
    """构造一封邮件。"""
    return GmailMessage(
        message_id="m1", sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms", body=body,
        email_timestamp="2026-10-12T09:00:00.000Z",
    )


def _failed(connection_id: str, *, error_code="GMAIL_MESSAGE_REJECTED"):
    """构造失败元数据。"""
    return create_unsuccessful_gmail_message_metadata(
        connection_id=connection_id, thread_id="t1", message=_message(),
        account_id=None, history_id="100", synced_at=NOW, error_code=error_code,
    )


# --------------------------------------------------------------------------
# 状态读取
# --------------------------------------------------------------------------


def test_unknown_message_has_no_state(fixture) -> None:
    """没有行时返回 None。"""
    handle, connection_id, _ = fixture
    assert load_existing_gmail_message_state(handle.connection, connection_id, "m1") is None


def test_null_content_hash_is_treated_as_no_state(fixture) -> None:
    """**``content_hash`` 为 NULL 的行视为"没有已处理状态"。**

    这类行没有可比较的内容状态，按"未处理"对待才能让它被重新处理。
    """
    handle, connection_id, _ = fixture
    handle.connection.execute(
        """
        INSERT INTO gmail_messages (
            connection_id, message_id, content_hash, parse_status, transaction_count
        ) VALUES (CAST(? AS UUID), 'm1', NULL, 'discovered', 0)
        """,
        [connection_id],
    )

    assert load_existing_gmail_message_state(handle.connection, connection_id, "m1") is None


def test_reads_back_the_persisted_state(fixture) -> None:
    """归档后可读回内容摘要、账户与状态。"""
    handle, connection_id, account_id = fixture
    from lyra.infrastructure.gmail.message_metadata import (
        create_parsed_gmail_message_metadata,
    )

    metadata = create_parsed_gmail_message_metadata(
        connection_id=connection_id, thread_id="t1", message=_message(),
        trade_count=2, account_id=account_id, history_id="100", synced_at=NOW,
    )
    from lyra.infrastructure.db.gmail_message_repository import insert_gmail_message_row

    insert_gmail_message_row(
        handle.connection, metadata, synced_at="2026-10-12T09:00:00.000Z"
    )

    state = load_existing_gmail_message_state(handle.connection, connection_id, "m1")
    assert state is not None
    assert state.content_hash == metadata.content_hash
    assert state.account_id == account_id
    # **parsed 而不是 imported**：升级为 imported 是导入计划做的事，
    # 元数据构造本身只标记到 parsed。
    assert state.parse_status == "parsed"


def test_state_is_scoped_by_connection(fixture) -> None:
    """状态按连接隔离：另一个连接读不到。"""
    handle, connection_id, _ = fixture
    persist_unsuccessful_gmail_message(
        handle.connection, _failed(connection_id), synced_at="2026-10-12T09:00:00.000Z"
    )

    other = str(uuid.uuid4())
    assert load_existing_gmail_message_state(handle.connection, other, "m1") is None


# --------------------------------------------------------------------------
# 失败归档
# --------------------------------------------------------------------------


def test_persists_a_rejected_message_without_a_body(fixture) -> None:
    """失败归档写入状态与错误码，且**没有正文字段可写**。"""
    handle, connection_id, _ = fixture
    metadata = _failed(connection_id)

    persist_unsuccessful_gmail_message(
        handle.connection, metadata, synced_at="2026-10-12T09:00:00.000Z"
    )

    row = handle.connection.execute(
        """
        SELECT parse_status, error_code, transaction_count, history_id, thread_id
        FROM gmail_messages WHERE message_id = 'm1'
        """
    ).fetchone()
    assert row is not None
    assert row[0] == "rejected"
    assert row[1] == "GMAIL_MESSAGE_REJECTED"
    assert row[2] == 0
    assert row[3] == "100"
    assert row[4] == "t1"


def test_mapping_conflict_is_recorded_as_conflict(fixture) -> None:
    """账户映射冲突记为 conflict。"""
    handle, connection_id, _ = fixture

    persist_unsuccessful_gmail_message(
        handle.connection, _failed(connection_id, error_code="ACCOUNT_MAPPING_REQUIRED"),
        synced_at="2026-10-12T09:00:00.000Z",
    )

    state = load_existing_gmail_message_state(handle.connection, connection_id, "m1")
    assert state is not None
    assert state.parse_status == "conflict"
    # 映射缺失时没有账户归属。
    assert state.account_id is None


def test_persisting_the_same_message_twice_conflicts(fixture) -> None:
    """**同一封邮件重复归档会撞主键。**

    参考实现用的是普通 INSERT（无 upsert），主键是 ``(connection_id, message_id)``。
    同步服务在失败路径上**无条件**调用归档，因此"同一封邮件在重叠窗口里被处理两次"
    会在这里抛错。

    这条测试固定真实行为；调用方若需要幂等重试，必须自己先查状态再决定是否归档。
    """
    handle, connection_id, _ = fixture
    metadata = _failed(connection_id)
    persist_unsuccessful_gmail_message(
        handle.connection, metadata, synced_at="2026-10-12T09:00:00.000Z"
    )

    with pytest.raises(duckdb.ConstraintException):
        persist_unsuccessful_gmail_message(
            handle.connection, metadata, synced_at="2026-10-12T10:00:00.000Z"
        )


def test_row_without_a_content_hash_violates_the_schema_check(fixture) -> None:
    """**非 ``discovered`` 状态必须有 64 位内容摘要**——schema 会拦住。"""
    handle, connection_id, _ = fixture

    with pytest.raises(duckdb.ConstraintException):
        handle.connection.execute(
            """
            INSERT INTO gmail_messages (
                connection_id, message_id, content_hash, parse_status, transaction_count
            ) VALUES (CAST(? AS UUID), 'm9', NULL, 'imported', 0)
            """,
            [connection_id],
        )


# --------------------------------------------------------------------------
# 导入的原子提交
# --------------------------------------------------------------------------


def _ready_plan(connection_id: str, account_id: str, *, body: str = "b"):
    """构造一个就绪的导入计划。"""
    from lyra.domain.gmail.econfirm import EmailTrade
    from lyra.infrastructure.gmail.message_import_plan import (
        build_gmail_message_import_plan,
    )

    return build_gmail_message_import_plan(
        connection_id=connection_id, thread_id="t1", message=_message(body),
        trades=(EmailTrade(
            trade_date="2026-10-12", action="Buy", symbol="INTC",
            description="INTEL CORP", quantity="10", price="40.5",
            fees="0", amount="-405",
        ),),
        account_id=account_id, ledger_account="TST001", history_id="100",
        synced_at=NOW, parser_version="2", base_ledger_version=0,
        existing_message=None, existing_account_transactions=(),
    )


def test_import_writes_ledger_message_and_links_together(fixture) -> None:
    """**账本交易、消息归档与关联在同一个事务里全部写入。**"""
    from lyra.infrastructure.db.gmail_message_repository import (
        persist_gmail_message_import,
    )

    handle, connection_id, account_id = fixture
    plan = _ready_plan(connection_id, account_id)

    result = persist_gmail_message_import(
        handle.connection, plan=plan, expected_ledger_version=0,
        synced_at="2026-10-12T09:00:00.000Z",
    )

    assert result.changed is True
    assert result.inserted_rows == 1
    assert result.ledger_version == 1

    # 交易入账。
    transactions = handle.connection.execute(
        "SELECT count(*) FROM transactions"
    ).fetchone()
    assert transactions is not None and transactions[0] == 1
    # 批次入账。
    lots = handle.connection.execute("SELECT count(*) FROM lots").fetchone()
    assert lots is not None and lots[0] == 1
    # 消息归档。
    state = load_existing_gmail_message_state(handle.connection, connection_id, "m1")
    assert state is not None
    assert state.parse_status == "imported"
    # 关联。
    links = handle.connection.execute(
        "SELECT ordinal, transaction_hash FROM gmail_message_transactions"
    ).fetchall()
    assert len(links) == 1
    assert links[0][0] == 1


def test_ledger_version_reason_distinguishes_gmail_from_files(fixture) -> None:
    """**账本版本的 reason 是 ``gmail_import``。**

    审计时需要区分交易来自文件还是邮件；写错会让"这笔交易怎么来的"无从回答。
    """
    from lyra.infrastructure.db.gmail_message_repository import (
        persist_gmail_message_import,
    )

    handle, connection_id, account_id = fixture
    persist_gmail_message_import(
        handle.connection, plan=_ready_plan(connection_id, account_id),
        expected_ledger_version=0, synced_at="2026-10-12T09:00:00.000Z",
    )

    reason, reference = handle.connection.execute(
        "SELECT reason, source_reference FROM ledger_versions"
    ).fetchone()
    assert reason == "gmail_import"
    assert reference == "m1"


def test_version_conflict_writes_nothing(fixture) -> None:
    """**账本版本冲突时什么都不写。**

    若先写了账本再发现冲突，那半次导入会永久留在库里，而调用方以为整体失败了。
    """
    from lyra.application.app_error import ApplicationError
    from lyra.infrastructure.db.gmail_message_repository import (
        persist_gmail_message_import,
    )

    handle, connection_id, account_id = fixture
    # 账本已有一个版本，因此期望 0 是过期的。
    handle.connection.execute(
        "INSERT INTO ledger_versions (reason, source_reference) VALUES ('file_import', 'x')"
    )

    with pytest.raises(ApplicationError) as raised:
        persist_gmail_message_import(
            handle.connection, plan=_ready_plan(connection_id, account_id),
            expected_ledger_version=0, synced_at="2026-10-12T09:00:00.000Z",
        )

    assert raised.value.code == "LEDGER_VERSION_CONFLICT"
    # 交易、批次、消息、关联全部为空。
    for table in ("transactions", "lots", "gmail_messages", "gmail_message_transactions"):
        count = handle.connection.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert count is not None and count[0] == 0, table


def test_second_import_of_the_same_message_is_skipped(fixture) -> None:
    """同一封邮件再次导入时被识别为已导入，账本不变。"""
    from lyra.domain.gmail.econfirm import EmailTrade
    from lyra.infrastructure.db.gmail_message_repository import (
        persist_gmail_message_import,
    )
    from lyra.infrastructure.gmail.message_import_plan import (
        AlreadyImportedGmailMessagePlan,
        build_gmail_message_import_plan,
    )

    handle, connection_id, account_id = fixture
    persist_gmail_message_import(
        handle.connection, plan=_ready_plan(connection_id, account_id),
        expected_ledger_version=0, synced_at="2026-10-12T09:00:00.000Z",
    )

    second = build_gmail_message_import_plan(
        connection_id=connection_id, thread_id="t1", message=_message(),
        trades=(EmailTrade(
            trade_date="2026-10-12", action="Buy", symbol="INTC",
            description="INTEL CORP", quantity="10", price="40.5",
            fees="0", amount="-405",
        ),),
        account_id=account_id, ledger_account="TST001", history_id="100",
        synced_at=NOW, parser_version="2", base_ledger_version=1,
        existing_message=load_existing_gmail_message_state(
            handle.connection, connection_id, "m1"
        ),
        existing_account_transactions=(),
    )

    assert isinstance(second, AlreadyImportedGmailMessagePlan)
    # 账本仍只有一个版本、一条交易。
    versions = handle.connection.execute("SELECT count(*) FROM ledger_versions").fetchone()
    assert versions is not None and versions[0] == 1
    transactions = handle.connection.execute("SELECT count(*) FROM transactions").fetchone()
    assert transactions is not None and transactions[0] == 1
