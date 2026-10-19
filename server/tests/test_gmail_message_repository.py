"""Gmail 消息归档仓储测试。

两条行为值得固定下来：

1. ``content_hash`` 为 NULL 的行视为"没有已处理状态"；
2. 消息行是**普通 INSERT**，主键 ``(connection_id, message_id)``，因此重复归档会
   撞主键。这条测试把真实行为写下来，避免以后无意中依赖某个未言明的调用顺序。

多用户下还固定三条隔离性质：

- 消息状态按**连接归属**读取，A 读不到 B 的邮件状态；
- 写入要求连接与账户同属当前用户，跨归属时一条都不写；
- 两个用户各自的 Gmail 连接收到**同一封邮件**时各写一行，**不互相去重**；
- 用户停用后，排队/在飞的同步**不得入账**（在事务内重查活动状态）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from lyra.application.app_error import ApplicationError
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
SYNCED_AT = "2026-10-12T09:00:00.000Z"
SUFFIX = "276"

USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture()
def fixture(tmp_path: Path):
    """建立两个用户、各自一个账户与一条 Gmail 连接。"""
    handle = open_lyra_database(str(tmp_path / "msg.duckdb"))
    accounts: dict[str, str] = {}
    connections: dict[str, str] = {}
    for user_id in (USER_A, USER_B):
        handle.connection.execute(
            "INSERT INTO users (id, display_name) VALUES (CAST(? AS UUID), ?)",
            [user_id, f"User {user_id[-1]}"],
        )
        account_id = str(uuid.uuid4())
        handle.connection.execute(
            """
            INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Account', 'TST001', ?)
            """,
            [account_id, user_id, SUFFIX],
        )
        accounts[user_id] = account_id
        connection_id = str(uuid.uuid4())
        handle.connection.execute(
            """
            INSERT INTO gmail_connections (
                id, user_id, google_subject, google_email, encrypted_refresh_token,
                granted_scopes, status
            ) VALUES (
                CAST(? AS UUID), CAST(? AS UUID), ?, ?, 'v1.x.y.z', 'scope', 'pending_initial_scan'
            )
            """,
            [connection_id, user_id, f"subject-{user_id[-1]}", f"{user_id[-1]}@b.com"],
        )
        connections[user_id] = connection_id
    try:
        yield handle, connections, accounts
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
    handle, connections, _ = fixture
    assert load_existing_gmail_message_state(
        handle.connection, USER_A, connections[USER_A], "m1"
    ) is None


def test_null_content_hash_is_treated_as_no_state(fixture) -> None:
    """**``content_hash`` 为 NULL 的行视为"没有已处理状态"。**

    这类行没有可比较的内容状态，按"未处理"对待才能让它被重新处理。
    """
    handle, connections, _ = fixture
    handle.connection.execute(
        """
        INSERT INTO gmail_messages (
            connection_id, message_id, content_hash, parse_status, transaction_count
        ) VALUES (CAST(? AS UUID), 'm1', NULL, 'discovered', 0)
        """,
        [connections[USER_A]],
    )

    assert load_existing_gmail_message_state(
        handle.connection, USER_A, connections[USER_A], "m1"
    ) is None


def test_reads_back_the_persisted_state(fixture) -> None:
    """归档后可读回内容摘要、账户与状态。"""
    handle, connections, accounts = fixture
    from lyra.infrastructure.gmail.message_metadata import (
        create_parsed_gmail_message_metadata,
    )

    metadata = create_parsed_gmail_message_metadata(
        connection_id=connections[USER_A], thread_id="t1", message=_message(),
        trade_count=2, account_id=accounts[USER_A], history_id="100", synced_at=NOW,
    )
    from lyra.infrastructure.db.gmail_message_repository import insert_gmail_message_row

    insert_gmail_message_row(
        handle.connection, USER_A, metadata, synced_at=SYNCED_AT
    )

    state = load_existing_gmail_message_state(
        handle.connection, USER_A, connections[USER_A], "m1"
    )
    assert state is not None
    assert state.content_hash == metadata.content_hash
    assert state.account_id == accounts[USER_A]
    # **parsed 而不是 imported**：升级为 imported 是导入计划做的事，
    # 元数据构造本身只标记到 parsed。
    assert state.parse_status == "parsed"


def test_state_is_scoped_by_connection(fixture) -> None:
    """状态按连接隔离：另一个连接读不到。"""
    handle, connections, _ = fixture
    persist_unsuccessful_gmail_message(
        handle.connection, USER_A, _failed(connections[USER_A]), synced_at=SYNCED_AT
    )

    other = str(uuid.uuid4())
    assert load_existing_gmail_message_state(
        handle.connection, USER_A, other, "m1"
    ) is None


def test_state_of_another_users_connection_is_not_visible(fixture) -> None:
    """**A 读不到 B 连接的邮件状态。**

    A 只要猜到 B 的连接 ID 与邮件 ID，就能读到 B 的账户归属与解析状态——这是一个
    真实的越权读取，而不是理论风险。
    """
    handle, connections, _ = fixture
    persist_unsuccessful_gmail_message(
        handle.connection, USER_B, _failed(connections[USER_B]), synced_at=SYNCED_AT
    )

    assert load_existing_gmail_message_state(
        handle.connection, USER_A, connections[USER_B], "m1"
    ) is None
    # B 自己仍能读到。
    assert load_existing_gmail_message_state(
        handle.connection, USER_B, connections[USER_B], "m1"
    ) is not None


def test_the_same_message_id_under_two_users_is_not_deduplicated(fixture) -> None:
    """**用户间相同邮件标识不互相去重。**

    主键是 ``(connection_id, message_id)``：同一封邮件（相同 ``message_id``）分别
    进入两个用户的连接时各写一行。若去重键只有 ``message_id``，先同步的一方会把
    后者的邮件当成"已处理"，后者永远收不到这笔交易。
    """
    handle, connections, _ = fixture
    persist_unsuccessful_gmail_message(
        handle.connection, USER_A, _failed(connections[USER_A]), synced_at=SYNCED_AT
    )
    persist_unsuccessful_gmail_message(
        handle.connection, USER_B, _failed(connections[USER_B]), synced_at=SYNCED_AT
    )

    rows = handle.connection.execute(
        "SELECT count(*) FROM gmail_messages WHERE message_id = 'm1'"
    ).fetchone()
    assert rows is not None and rows[0] == 2
    # 两个用户各自读到自己那条。
    assert load_existing_gmail_message_state(
        handle.connection, USER_A, connections[USER_A], "m1"
    ) is not None
    assert load_existing_gmail_message_state(
        handle.connection, USER_B, connections[USER_B], "m1"
    ) is not None


# --------------------------------------------------------------------------
# 失败归档
# --------------------------------------------------------------------------


def test_persists_a_rejected_message_without_a_body(fixture) -> None:
    """失败归档写入状态与错误码，且**没有正文字段可写**。"""
    handle, connections, _ = fixture
    metadata = _failed(connections[USER_A])

    persist_unsuccessful_gmail_message(
        handle.connection, USER_A, metadata, synced_at=SYNCED_AT
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
    handle, connections, _ = fixture

    persist_unsuccessful_gmail_message(
        handle.connection, USER_A,
        _failed(connections[USER_A], error_code="ACCOUNT_MAPPING_REQUIRED"),
        synced_at=SYNCED_AT,
    )

    state = load_existing_gmail_message_state(
        handle.connection, USER_A, connections[USER_A], "m1"
    )
    assert state is not None
    assert state.parse_status == "conflict"
    # 映射缺失时没有账户归属。
    assert state.account_id is None


def test_archiving_into_another_users_connection_writes_nothing(fixture) -> None:
    """**不能往 B 的连接里归档消息。** 归属不满足时一行都不写。"""
    handle, connections, _ = fixture

    with pytest.raises(ApplicationError) as raised:
        persist_unsuccessful_gmail_message(
            handle.connection, USER_A, _failed(connections[USER_B]), synced_at=SYNCED_AT
        )

    assert raised.value.status_code == 404
    count = handle.connection.execute(
        "SELECT count(*) FROM gmail_messages"
    ).fetchone()
    assert count is not None and count[0] == 0


def test_archiving_with_another_users_account_writes_nothing(fixture) -> None:
    """**A 的连接不能挂上 B 的账户**：消息行需要账户与连接同属一个用户。"""
    handle, connections, accounts = fixture
    from lyra.infrastructure.gmail.message_metadata import (
        create_parsed_gmail_message_metadata,
    )

    metadata = create_parsed_gmail_message_metadata(
        connection_id=connections[USER_A], thread_id="t1", message=_message(),
        trade_count=1, account_id=accounts[USER_B], history_id="100", synced_at=NOW,
    )

    with pytest.raises(ApplicationError) as raised:
        handle.connection.execute("BEGIN TRANSACTION")
        try:
            from lyra.infrastructure.db.gmail_message_repository import (
                insert_gmail_message_row,
            )

            insert_gmail_message_row(
                handle.connection, USER_A, metadata, synced_at=SYNCED_AT
            )
        finally:
            handle.connection.execute("ROLLBACK")

    assert raised.value.status_code == 404
    count = handle.connection.execute(
        "SELECT count(*) FROM gmail_messages"
    ).fetchone()
    assert count is not None and count[0] == 0


def test_persisting_the_same_failed_message_twice_updates_its_status(fixture) -> None:
    """重叠扫描重复归档失败邮件时更新状态，而不是撞消息主键。"""
    handle, connections, _ = fixture
    metadata = _failed(connections[USER_A])
    persist_unsuccessful_gmail_message(
        handle.connection, USER_A, metadata, synced_at=SYNCED_AT
    )

    persist_unsuccessful_gmail_message(
        handle.connection, USER_A, metadata,
        synced_at="2026-10-12T10:00:00.000Z",
    )

    row = handle.connection.execute(
        "SELECT count(*), max(synced_at) FROM gmail_messages "
        "WHERE connection_id = CAST(? AS UUID) AND message_id = 'm1'",
        [connections[USER_A]],
    ).fetchone()
    assert row is not None and row[0] == 1
    assert row[1].astimezone(UTC).isoformat().startswith("2026-10-12T10:00:00")


def test_failed_message_can_become_imported_after_mapping_is_fixed(fixture) -> None:
    """失败时空账户归属可在修复映射后更新成导入状态。"""
    from lyra.infrastructure.db.gmail_message_repository import (
        persist_gmail_message_import,
    )

    handle, connections, accounts = fixture
    persist_unsuccessful_gmail_message(
        handle.connection, USER_A,
        _failed(connections[USER_A], error_code="ACCOUNT_MAPPING_REQUIRED"),
        synced_at=SYNCED_AT,
    )
    plan = _ready_plan(connections[USER_A], accounts[USER_A])

    result = persist_gmail_message_import(
        handle.connection, user_id=USER_A, plan=plan,
        expected_ledger_version=0, synced_at=SYNCED_AT,
    )

    assert result.inserted_rows == 1
    state = load_existing_gmail_message_state(
        handle.connection, USER_A, connections[USER_A], "m1"
    )
    assert state is not None
    assert state.parse_status == "imported"
    assert state.account_id == accounts[USER_A]


def test_imported_message_cannot_be_downgraded_by_a_late_failure(fixture) -> None:
    """后到失败归档不得覆盖已提交的消息与交易关联。"""
    from lyra.infrastructure.db.gmail_message_repository import (
        persist_gmail_message_import,
    )

    handle, connections, accounts = fixture
    plan = _ready_plan(connections[USER_A], accounts[USER_A])
    persist_gmail_message_import(
        handle.connection, user_id=USER_A, plan=plan,
        expected_ledger_version=0, synced_at=SYNCED_AT,
    )

    with pytest.raises(ApplicationError) as raised:
        persist_unsuccessful_gmail_message(
            handle.connection, USER_A,
            _failed(connections[USER_A]), synced_at=SYNCED_AT,
        )

    assert raised.value.code == "GMAIL_MESSAGE_CONFLICT"
    state = load_existing_gmail_message_state(
        handle.connection, USER_A, connections[USER_A], "m1"
    )
    assert state is not None and state.parse_status == "imported"
    links = handle.connection.execute(
        "SELECT count(*) FROM gmail_message_transactions"
    ).fetchone()
    assert links is not None and links[0] == 1


def test_occurrence_maxima_are_loaded_only_for_the_user_account(fixture) -> None:
    """跨邮件 occurrence 高水位来自该用户账户已保存的 Gmail 关联。"""
    from lyra.infrastructure.db.gmail_message_repository import (
        load_gmail_transaction_occurrence_maxima,
        persist_gmail_message_import,
    )

    handle, connections, accounts = fixture
    plan = _ready_plan(connections[USER_A], accounts[USER_A])
    business_hash = plan.commit.inserted_transactions[0].business_hash
    persist_gmail_message_import(
        handle.connection, user_id=USER_A, plan=plan,
        expected_ledger_version=0, synced_at=SYNCED_AT,
    )

    assert load_gmail_transaction_occurrence_maxima(
        handle.connection, USER_A, accounts[USER_A]
    ) == {business_hash: 1}
    assert load_gmail_transaction_occurrence_maxima(
        handle.connection, USER_B, accounts[USER_A]
    ) == {}
    assert load_gmail_transaction_occurrence_maxima(
        handle.connection, USER_A, accounts[USER_B]
    ) == {}


def test_row_without_a_content_hash_violates_the_schema_check(fixture) -> None:
    """**非 ``discovered`` 状态必须有 64 位内容摘要**——schema 会拦住。"""
    handle, connections, _ = fixture

    with pytest.raises(duckdb.ConstraintException):
        handle.connection.execute(
            """
            INSERT INTO gmail_messages (
                connection_id, message_id, content_hash, parse_status, transaction_count
            ) VALUES (CAST(? AS UUID), 'm9', NULL, 'imported', 0)
            """,
            [connections[USER_A]],
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

    handle, connections, accounts = fixture
    plan = _ready_plan(connections[USER_A], accounts[USER_A])

    result = persist_gmail_message_import(
        handle.connection, user_id=USER_A, plan=plan, expected_ledger_version=0,
        synced_at=SYNCED_AT,
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
    state = load_existing_gmail_message_state(
        handle.connection, USER_A, connections[USER_A], "m1"
    )
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

    handle, connections, accounts = fixture
    persist_gmail_message_import(
        handle.connection, user_id=USER_A,
        plan=_ready_plan(connections[USER_A], accounts[USER_A]),
        expected_ledger_version=0, synced_at=SYNCED_AT,
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
    from lyra.infrastructure.db.gmail_message_repository import (
        persist_gmail_message_import,
    )

    handle, connections, accounts = fixture
    # 该用户已有一个版本，因此期望 0 是过期的。
    handle.connection.execute(
        """
        INSERT INTO ledger_versions (user_id, reason, source_reference)
        VALUES (CAST(? AS UUID), 'file_import', 'x')
        """,
        [USER_A],
    )

    with pytest.raises(ApplicationError) as raised:
        persist_gmail_message_import(
            handle.connection, user_id=USER_A,
            plan=_ready_plan(connections[USER_A], accounts[USER_A]),
            expected_ledger_version=0, synced_at=SYNCED_AT,
        )

    assert raised.value.code == "LEDGER_VERSION_CONFLICT"
    # 交易、批次、消息、关联全部为空。
    for table in ("transactions", "lots", "gmail_messages", "gmail_message_transactions"):
        count = handle.connection.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert count is not None and count[0] == 0, table


def test_another_users_ledger_version_does_not_invalidate_the_import(fixture) -> None:
    """**B 的账本版本不影响 A 的导入基准**（契约 §3.3）。"""
    from lyra.infrastructure.db.gmail_message_repository import (
        persist_gmail_message_import,
    )

    handle, connections, accounts = fixture
    handle.connection.execute(
        """
        INSERT INTO ledger_versions (user_id, reason, source_reference)
        VALUES (CAST(? AS UUID), 'file_import', 'b-file')
        """,
        [USER_B],
    )

    result = persist_gmail_message_import(
        handle.connection, user_id=USER_A,
        plan=_ready_plan(connections[USER_A], accounts[USER_A]),
        expected_ledger_version=0, synced_at=SYNCED_AT,
    )

    assert result.changed is True
    assert result.inserted_rows == 1


def test_import_into_another_users_account_writes_nothing(fixture) -> None:
    """**A 的邮件不能写进 B 的账户**：一条交易、一行消息都不落库。"""
    from lyra.infrastructure.db.gmail_message_repository import (
        persist_gmail_message_import,
    )

    handle, connections, accounts = fixture
    plan = _ready_plan(connections[USER_A], accounts[USER_B])

    with pytest.raises(ApplicationError) as raised:
        persist_gmail_message_import(
            handle.connection, user_id=USER_A, plan=plan, expected_ledger_version=0,
            synced_at=SYNCED_AT,
        )

    assert raised.value.status_code == 409
    for table in ("transactions", "lots", "gmail_messages", "gmail_message_transactions", "ledger_versions"):
        count = handle.connection.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert count is not None and count[0] == 0, table


def test_disabled_user_cannot_import(fixture) -> None:
    """**用户停用后，排队/在飞的同步不得入账。**

    "排队"指请求在等待或在执行中；停用与该事务在共享连接的显式事务锁上排序，因此
    停用提交后，入账前的最后一次活动状态检查必然读到 ``disabled``，事务整体回滚
    （P0 契约 §3.5、计划 §4.4）。用异常而不是静默跳过：调用方必须知道这次同步没入账。
    """
    from lyra.infrastructure.db.gmail_message_repository import (
        persist_gmail_message_import,
    )

    handle, connections, accounts = fixture
    handle.connection.execute(
        "UPDATE users SET status = 'disabled' WHERE id = CAST(? AS UUID)", [USER_A]
    )
    plan = _ready_plan(connections[USER_A], accounts[USER_A])

    with pytest.raises(ApplicationError) as raised:
        persist_gmail_message_import(
            handle.connection, user_id=USER_A, plan=plan, expected_ledger_version=0,
            synced_at=SYNCED_AT,
        )

    assert raised.value.code == "USER_DISABLED"
    assert raised.value.status_code == 403
    assert raised.value.retryable is False
    # 账本、消息与关联全部为空——没有部分写入。
    for table in ("transactions", "lots", "gmail_messages", "gmail_message_transactions", "ledger_versions"):
        count = handle.connection.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert count is not None and count[0] == 0, table


def test_unknown_user_cannot_import(fixture) -> None:
    """归属用户不存在时同样拒绝，不猜测归属。"""
    from lyra.infrastructure.db.gmail_message_repository import (
        persist_gmail_message_import,
    )

    handle, connections, accounts = fixture
    plan = _ready_plan(connections[USER_A], accounts[USER_A])

    with pytest.raises(ApplicationError) as raised:
        persist_gmail_message_import(
            handle.connection, user_id=str(uuid.uuid4()), plan=plan,
            expected_ledger_version=0, synced_at=SYNCED_AT,
        )

    assert raised.value.code == "USER_DISABLED"
    count = handle.connection.execute("SELECT count(*) FROM gmail_messages").fetchone()
    assert count is not None and count[0] == 0


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

    handle, connections, accounts = fixture
    persist_gmail_message_import(
        handle.connection, user_id=USER_A,
        plan=_ready_plan(connections[USER_A], accounts[USER_A]),
        expected_ledger_version=0, synced_at=SYNCED_AT,
    )

    second = build_gmail_message_import_plan(
        connection_id=connections[USER_A], thread_id="t1", message=_message(),
        trades=(EmailTrade(
            trade_date="2026-10-12", action="Buy", symbol="INTC",
            description="INTEL CORP", quantity="10", price="40.5",
            fees="0", amount="-405",
        ),),
        account_id=accounts[USER_A], ledger_account="TST001", history_id="100",
        synced_at=NOW, parser_version="2", base_ledger_version=1,
        existing_message=load_existing_gmail_message_state(
            handle.connection, USER_A, connections[USER_A], "m1"
        ),
        existing_account_transactions=(),
    )

    assert isinstance(second, AlreadyImportedGmailMessagePlan)
    # 账本仍只有一个版本、一条交易。
    versions = handle.connection.execute("SELECT count(*) FROM ledger_versions").fetchone()
    assert versions is not None and versions[0] == 1
    transactions = handle.connection.execute("SELECT count(*) FROM transactions").fetchone()
    assert transactions is not None and transactions[0] == 1


def test_the_same_message_imports_independently_for_each_user(fixture) -> None:
    """**两个用户收到同一封邮件时各自入账，不互相去重。**

    同一 ``message_id`` 在两条连接下各有一行状态；第二次导入不会读到第一次的状态，
    因此两个账本各自得到一笔交易与一个账本版本。
    """
    from lyra.infrastructure.db.gmail_message_repository import (
        persist_gmail_message_import,
    )

    handle, connections, accounts = fixture

    first = persist_gmail_message_import(
        handle.connection, user_id=USER_A,
        plan=_ready_plan(connections[USER_A], accounts[USER_A]),
        expected_ledger_version=0, synced_at=SYNCED_AT,
    )
    second = persist_gmail_message_import(
        handle.connection, user_id=USER_B,
        plan=_ready_plan(connections[USER_B], accounts[USER_B]),
        expected_ledger_version=0, synced_at=SYNCED_AT,
    )

    assert first.changed is True and second.changed is True
    assert first.inserted_rows == 1 and second.inserted_rows == 1
    # 每个用户在各自的账本里看到恰好一条交易。
    for account_id in (accounts[USER_A], accounts[USER_B]):
        count = handle.connection.execute(
            "SELECT count(*) FROM transactions WHERE account_id = CAST(? AS UUID)",
            [account_id],
        ).fetchone()
        assert count is not None and count[0] == 1
    # 每个用户各自一条版本，且都带自己的归属。
    owners = handle.connection.execute(
        "SELECT user_id FROM ledger_versions ORDER BY version"
    ).fetchall()
    assert [str(row[0]) for row in owners] == [USER_A, USER_B]
