"""schema 16 物理结构与 schema 15 → 16 升级路径的验证。

本文件回答两个问题：

1. **schema 16 的约束是否真的存在且真的生效**——多用户隔离里唯一能由数据库保证
   「子资源与父资源同属一个用户」的手段就是复合外键（P0 契约 §3.2），因此这里不止
   断言约束文本存在，还用真实的越权写入去撞它。
2. **schema 15 的旧库能否被无损升级**——用**冻结夹具**
   ``fixtures/typescript-ledger-schema15.duckdb`` 的副本作为真实旧库（它由迁移前的
   TypeScript 服务端创建），灌入覆盖全部 24 张表的合成数据后升级，再逐项核对归属、
   金额与行数。

夹具本身**只读复制、绝不修改**。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
import pytest

from lyra.db.connection import open_lyra_database
from lyra.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    MigrationContext,
    MigrationError,
    MigrationOwnerIdentity,
    apply_migrations,
)

# **冻结夹具**：迁移前由 TypeScript 服务端创建，schema 15、28 个对象、无数据。
FIXTURE_DATABASE = (
    Path(__file__).resolve().parent / "fixtures" / "typescript-ledger-schema15.duckdb"
)

OWNER_USER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_USER_ID = "22222222-2222-2222-2222-222222222222"
OWNER_ACCOUNT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_ACCOUNT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaab"
OWNER_DEVICE_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OWNER_IDENTITY_ID = "cccccccc-cccc-cccc-cccc-ccccccccccc1"
OTHER_IDENTITY_ID = "cccccccc-cccc-cccc-cccc-ccccccccccc2"
OWNER_SESSION_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeee1"
OWNER_MESSAGE_ID = "ffffffff-ffff-ffff-ffff-fffffffffff1"
OWNER_IMPORT_ID = "99999999-9999-9999-9999-999999999991"
OWNER_GMAIL_ID = "88888888-8888-8888-8888-888888888881"

# 这些表经 account 或设备的**不可变归属**间接隔离，因此按契约 §3.2 刻意不带 user_id。
# 一条断言跑遍全部，避免今后有人"顺手"给某张子表补一个 user_id 而没人发现。
TABLES_WITHOUT_USER_ID = (
    "ledger_version_accounts",
    "transactions",
    "lots",
    "realized",
    "account_import_reminder_settings",
    "gmail_account_mappings",
    "gmail_messages",
    "gmail_message_transactions",
    "device_refresh_credentials",
    "device_access_tokens",
    "mutation_idempotency_records",
    "agent_sessions",
    "agent_messages",
    "agent_tool_calls",
)

VIEW_NAMES = (
    "v_positions",
    "v_realized_summary",
    "v_cashflows",
    "v_official_import_reminders",
)


@pytest.fixture()
def schema16(tmp_path: Path):
    """一个全新的 schema 16 库；用于物理结构与约束行为断言。"""
    database = open_lyra_database(str(tmp_path / "fresh.duckdb"))
    try:
        yield database.connection
    finally:
        database.close()


def _table_names(connection: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }


def _view_names(connection: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT view_name FROM duckdb_views() WHERE NOT internal"
        ).fetchall()
    }


def _column_names(connection: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table],
        ).fetchall()
    }


def _constraint_texts(connection: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT constraint_text FROM duckdb_constraints() WHERE table_name = ?",
            [table],
        ).fetchall()
    }


def _count(connection: duckdb.DuckDBPyConnection, table: str) -> int:
    row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def _insert_user(
    connection: duckdb.DuckDBPyConnection,
    user_id: str = OWNER_USER_ID,
    *,
    role: str = "member",
    display_name: str = "Owner",
) -> None:
    connection.execute(
        """
        INSERT INTO users (id, display_name, status, role)
        VALUES (CAST(? AS UUID), ?, 'active', ?)
        """,
        [user_id, display_name, role],
    )


def _insert_identity(
    connection: duckdb.DuckDBPyConnection,
    identity_id: str = OWNER_IDENTITY_ID,
    user_id: str = OWNER_USER_ID,
    *,
    provider: str = "google",
    subject: str = "subject-1",
) -> None:
    connection.execute(
        """
        INSERT INTO user_identities (id, user_id, provider, subject)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), ?, ?)
        """,
        [identity_id, user_id, provider, subject],
    )


def _insert_account(
    connection: duckdb.DuckDBPyConnection,
    account_id: str,
    user_id: str,
    *,
    legacy_account_key: str = "ACC-1",
) -> None:
    connection.execute(
        """
        INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Main', ?, '1234')
        """,
        [account_id, user_id, legacy_account_key],
    )


def _owner_context(
    *,
    identities: tuple[MigrationOwnerIdentity, ...] | None = None,
    owner_user_id: str = OWNER_USER_ID,
    owner_display_name: str = "Owner",
    owner_status: str = "active",
    owner_role: str = "admin",
) -> MigrationContext:
    """构造一份显式归属确认；默认含一条身份并把它指定为设备身份来源。"""
    if identities is None:
        identities = (
            MigrationOwnerIdentity(
                provider="google",
                subject="112233",
                email="owner@example.com",
                display_name="Owner",
                is_device_identity=True,
            ),
            MigrationOwnerIdentity(provider="github", subject="445566"),
        )
    return MigrationContext(
        owner_user_id=owner_user_id,
        owner_display_name=owner_display_name,
        identities=identities,
        owner_status=owner_status,
        owner_role=owner_role,
    )


def _seed_schema15(path: Path) -> None:
    """把冻结的 schema 15 夹具复制到 ``path`` 并灌入覆盖全部表的合成数据。"""
    shutil.copy2(FIXTURE_DATABASE, path)
    connection = duckdb.connect(str(path))
    try:
        # 旧运行时用于记录私有 owner 的登录身份；v16 读取它做归属回填后即删除该表，
        # 因此迁移后它不应再存在。
        connection.execute(
            """
            INSERT INTO server_owner_identity
                (singleton_key, provider, subject, email, display_name, created_at, updated_at)
            VALUES (1, 'google', '112233', 'owner@example.com', 'Owner',
                    current_timestamp, current_timestamp)
            """
        )
        connection.execute(
            """
            INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
            VALUES (CAST(? AS UUID), 'Main', 'ACC-1', '1234')
            """,
            [OWNER_ACCOUNT_ID],
        )
        connection.execute(
            """
            INSERT INTO ledger_versions (reason, source_reference)
            VALUES ('file_import', 'legacy.csv')
            """
        )
        connection.execute(
            """
            INSERT INTO import_files
                (id, file_hash, original_file_name, file_type, file_size_bytes,
                 parser_version, status, target_account_id, preview_ledger_version,
                 total_rows, inserted_rows, created_at, updated_at)
            VALUES (CAST(? AS UUID), repeat('a', 64), 'legacy.csv', 'csv', 2048,
                    'v1', 'committed', CAST(? AS UUID), 1, 3, 3,
                    current_timestamp, current_timestamp)
            """,
            [OWNER_IMPORT_ID, OWNER_ACCOUNT_ID],
        )
        connection.execute(
            """
            INSERT INTO transactions
                (account_id, record_date, transaction_date, action, raw_symbol, description,
                 asset_type, underlying, quantity, price, fees, amount, source_type,
                 source_reference, import_file_id, source_hash, sequence, occurrence,
                 business_hash, transaction_hash, parser_version, ledger_version)
            VALUES (CAST(? AS UUID), DATE '2026-01-02', DATE '2026-01-02', 'Buy', 'AAPL',
                    'BUY AAPL', 'stock', 'AAPL', 10, 100.123456, 1.5, -1001.7345, 'csv',
                    'legacy.csv', CAST(? AS UUID), repeat('b', 64), 1, 1, repeat('c', 64),
                    repeat('d', 64), 'v1', 1)
            """,
            [OWNER_ACCOUNT_ID, OWNER_IMPORT_ID],
        )
        connection.execute(
            """
            INSERT INTO lots
                (account_id, symbol_key, asset_type, underlying, direction, open_date,
                 open_price, open_quantity, remaining_quantity, open_transaction_hash,
                 ledger_version)
            VALUES (CAST(? AS UUID), 'AAPL', 'stock', 'AAPL', 'long', DATE '2026-01-02',
                    100.123456, 10, 4, repeat('d', 64), 1)
            """,
            [OWNER_ACCOUNT_ID],
        )
        # 平仓侧交易：realized 的 close_transaction_hash 必须指向真实存在的交易。
        connection.execute(
            """
            INSERT INTO transactions
                (account_id, record_date, transaction_date, action, raw_symbol, description,
                 asset_type, underlying, quantity, price, fees, amount, source_type,
                 source_reference, import_file_id, source_hash, sequence, occurrence,
                 business_hash, transaction_hash, parser_version, ledger_version)
            VALUES (CAST(? AS UUID), DATE '2026-02-02', DATE '2026-02-02', 'Sell', 'AAPL',
                    'SELL AAPL', 'stock', 'AAPL', 6, 110.5, 1.5, 661.5, 'csv',
                    'legacy.csv', CAST(? AS UUID), repeat('1', 64), 2, 1, repeat('2', 64),
                    repeat('e', 64), 'v1', 1)
            """,
            [OWNER_ACCOUNT_ID, OWNER_IMPORT_ID],
        )
        connection.execute(
            """
            INSERT INTO realized
                (account_id, symbol_key, asset_type, underlying, direction, open_date,
                 close_date, quantity, open_price, close_price, pnl, close_action,
                 open_transaction_hash, close_transaction_hash, ledger_version)
            VALUES (CAST(? AS UUID), 'AAPL', 'stock', 'AAPL', 'long', DATE '2026-01-02',
                    DATE '2026-02-02', 6, 100.123456, 110.5, 62.2593, 'Sell',
                    repeat('d', 64), repeat('e', 64), 1)
            """,
            [OWNER_ACCOUNT_ID],
        )
        connection.execute(
            """
            INSERT INTO account_import_reminder_settings (account_id, enabled, interval_months)
            VALUES (CAST(? AS UUID), true, 2)
            """,
            [OWNER_ACCOUNT_ID],
        )
        connection.execute(
            """
            INSERT INTO paired_devices
                (id, display_name, public_key_pem, public_key_fingerprint, key_algorithm,
                 status, revoked_at)
            VALUES (CAST(? AS UUID), 'Pixel 9', 'pem', repeat('f', 64), 'ed25519',
                    'revoked', current_timestamp)
            """,
            [OWNER_DEVICE_ID],
        )
        connection.execute(
            """
            INSERT INTO device_refresh_credentials
                (id, device_id, family_id, generation, token_hash, status, expires_at,
                 rotated_at)
            VALUES ('1a1a1a1a-1a1a-1a1a-1a1a-1a1a1a1a1a1a', CAST(? AS UUID),
                    '2b2b2b2b-2b2b-2b2b-2b2b-2b2b2b2b2b2b', 1, repeat('1', 64), 'rotated',
                    current_timestamp + INTERVAL 30 DAY, current_timestamp)
            """,
            [OWNER_DEVICE_ID],
        )
        connection.execute(
            """
            INSERT INTO device_access_tokens
                (id, device_id, token_hash, status, expires_at, revoked_at)
            VALUES ('3c3c3c3c-3c3c-3c3c-3c3c-3c3c3c3c3c3c', CAST(? AS UUID), repeat('2', 64),
                    'revoked', current_timestamp + INTERVAL 15 MINUTE, current_timestamp)
            """,
            [OWNER_DEVICE_ID],
        )
        connection.execute(
            """
            INSERT INTO mutation_idempotency_records
                (id, device_id, operation, key_hash, request_hash, status,
                 result_resource_id, response_status_code, created_at, expires_at, completed_at)
            VALUES ('4d4d4d4d-4d4d-4d4d-4d4d-4d4d4d4d4d4d', CAST(? AS UUID),
                    'accounts.create', repeat('3', 64), repeat('4', 64), 'completed',
                    CAST(? AS UUID), 201, current_timestamp, current_timestamp + INTERVAL 1 DAY,
                    current_timestamp)
            """,
            [OWNER_DEVICE_ID, OWNER_ACCOUNT_ID],
        )
        connection.execute(
            """
            INSERT INTO audit_events
                (id, occurred_at, event_type, outcome, actor_type, actor_id, target_type,
                 target_id, account_id, ledger_version, metadata_json)
            VALUES ('5e5e5e5e-5e5e-5e5e-5e5e-5e5e5e5e5e5e', current_timestamp,
                    'account.created', 'success', 'device', CAST(? AS UUID), 'account',
                    CAST(? AS UUID), CAST(? AS UUID), 1, '{}')
            """,
            [OWNER_DEVICE_ID, OWNER_ACCOUNT_ID, OWNER_ACCOUNT_ID],
        )
        connection.execute(
            "INSERT INTO agent_session_identities (id) VALUES (CAST(? AS UUID))",
            [OWNER_SESSION_ID],
        )
        connection.execute(
            """
            INSERT INTO agent_sessions
                (id, device_id, provider, status, account_scope_kind, account_id,
                 ledger_version, as_of, revision, closed_at)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), 'genkit', 'closed',
                    'account', CAST(? AS UUID), 1, current_timestamp, 1, current_timestamp)
            """,
            [OWNER_SESSION_ID, OWNER_DEVICE_ID, OWNER_ACCOUNT_ID],
        )
        connection.execute(
            """
            INSERT INTO agent_messages
                (id, session_id, ordinal, role, provider, status, content, created_at, completed_at)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), 1, 'user', 'genkit', 'completed',
                    'hello', current_timestamp, current_timestamp)
            """,
            [OWNER_MESSAGE_ID, OWNER_SESSION_ID],
        )
        connection.execute(
            """
            INSERT INTO agent_tool_calls
                (id, session_id, message_id, ordinal, tool_name, authorization_decision,
                 status, ledger_version, started_at, completed_at)
            VALUES ('6f6f6f6f-6f6f-6f6f-6f6f-6f6f6f6f6f6f', CAST(? AS UUID), CAST(? AS UUID),
                    1, 'ledger_positions', 'allowed', 'completed', 1,
                    current_timestamp, current_timestamp)
            """,
            [OWNER_SESSION_ID, OWNER_MESSAGE_ID],
        )
        connection.execute(
            """
            INSERT INTO gmail_connections
                (id, google_subject, google_email, encrypted_refresh_token, granted_scopes,
                 status)
            VALUES (CAST(? AS UUID), 'google-subject-1', 'owner@example.com', 'ciphertext',
                    'gmail.readonly', 'active')
            """,
            [OWNER_GMAIL_ID],
        )
        connection.execute(
            """
            INSERT INTO gmail_account_mappings
                (id, connection_id, account_suffix, account_id)
            VALUES ('7a7a7a7a-7a7a-7a7a-7a7a-7a7a7a7a7a7a', CAST(? AS UUID), '1234',
                    CAST(? AS UUID))
            """,
            [OWNER_GMAIL_ID, OWNER_ACCOUNT_ID],
        )
        connection.execute(
            """
            INSERT INTO gmail_messages
                (connection_id, message_id, content_hash, account_id, parse_status,
                 transaction_count)
            VALUES (CAST(? AS UUID), 'message-1', repeat('9', 64), CAST(? AS UUID),
                    'imported', 1)
            """,
            [OWNER_GMAIL_ID, OWNER_ACCOUNT_ID],
        )
        connection.execute(
            """
            INSERT INTO gmail_message_transactions
                (connection_id, message_id, account_id, transaction_hash, ordinal)
            VALUES (CAST(? AS UUID), 'message-1', CAST(? AS UUID), repeat('d', 64), 1)
            """,
            [OWNER_GMAIL_ID, OWNER_ACCOUNT_ID],
        )
        connection.execute(
            "INSERT INTO market_source_settings VALUES (1, 7, '{\"stock\":[\"alpaca\"]}'::JSON, current_timestamp)"
        )
    finally:
        connection.close()


# =====================================================================
# 表清单与约束落点
# =====================================================================


def test_schema_version_is_sixteen(schema16: duckdb.DuckDBPyConnection) -> None:
    """全新库必须停在 schema 16，且没有留下任何重建残留。"""
    row = schema16.execute("SELECT max(version) FROM schema_migrations").fetchone()
    assert row is not None and int(row[0]) == CURRENT_SCHEMA_VERSION == 17
    leftovers = {
        str(row[0])
        for row in schema16.execute(
            "SELECT table_name FROM duckdb_tables() "
            "WHERE table_name LIKE '_stage%' OR table_name LIKE '_migration%'"
        ).fetchall()
    }
    assert leftovers == set()


def test_users_are_a_single_mutable_table(schema16: duckdb.DuckDBPyConnection) -> None:
    """users 必须是普通可变表：status/role 直接放在上面，不拆成两张表。"""
    columns = _column_names(schema16, "users")
    assert {"id", "display_name", "status", "role", "created_at", "updated_at"} <= columns
    assert "user_status" not in _table_names(schema16)


def test_user_status_update_survives_child_references(
    schema16: duckdb.DuckDBPyConnection,
) -> None:
    """有设备/账户/版本引用时更新 users 的非键列必须成功（停用的核心操作）。"""
    _insert_user(schema16)
    _insert_identity(schema16)
    _insert_account(schema16, OWNER_ACCOUNT_ID, OWNER_USER_ID)
    schema16.execute(
        """
        INSERT INTO paired_devices
            (id, user_id, identity_id, display_name, public_key_pem,
             public_key_fingerprint, key_algorithm)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), CAST(? AS UUID), 'Pixel', 'pem',
                repeat('f', 64), 'ed25519')
        """,
        [OWNER_DEVICE_ID, OWNER_USER_ID, OWNER_IDENTITY_ID],
    )
    schema16.execute(
        "INSERT INTO ledger_versions (reason, source_reference, user_id) "
        "VALUES ('file_import', 'x', CAST(? AS UUID))",
        [OWNER_USER_ID],
    )

    schema16.execute(
        "UPDATE users SET status = 'disabled' WHERE id = CAST(? AS UUID)", [OWNER_USER_ID]
    )

    row = schema16.execute(
        "SELECT status FROM users WHERE id = CAST(? AS UUID)", [OWNER_USER_ID]
    ).fetchone()
    assert row is not None and row[0] == "disabled"


def test_user_identities_has_both_unique_keys(schema16: duckdb.DuckDBPyConnection) -> None:
    """身份表必须有全局 (provider, subject) 唯一，以及供复合外键使用的 (user_id, id)。

    缺任何一个都会让迁移在实际库里失败：前者是并发收敛的唯一依据，后者是
    paired_devices 复合外键的父侧前提（DuckDB 的 F3）。
    """
    texts = _constraint_texts(schema16, "user_identities")
    assert "UNIQUE(provider, subject)" in texts
    assert "UNIQUE(user_id, id)" in texts


def test_accounts_has_both_unique_keys(schema16: duckdb.DuckDBPyConnection) -> None:
    """账户表：用户内 legacy key 唯一 + 供复合外键使用的 (user_id, id)。"""
    texts = _constraint_texts(schema16, "accounts")
    assert "UNIQUE(user_id, legacy_account_key)" in texts
    assert "UNIQUE(user_id, id)" in texts
    # 旧的全局唯一键必须消失，否则 B 无法拥有与 A 同名的账户。
    assert "UNIQUE(legacy_account_key)" not in texts


def test_composite_parent_keys_exist_where_referenced(
    schema16: duckdb.DuckDBPyConnection,
) -> None:
    """schema 16 里所有被复合外键引用的父表都必须带 (user_id, id)。"""
    for table in ("user_identities", "accounts", "gmail_connections", "agent_session_identities"):
        assert "UNIQUE(user_id, id)" in _constraint_texts(schema16, table), table


def test_child_tables_do_not_carry_user_id(schema16: duckdb.DuckDBPyConnection) -> None:
    """经 account/设备/会话隔离的表**不得**机械地再加 user_id（契约 §3.2）。"""
    for table in TABLES_WITHOUT_USER_ID:
        assert "user_id" not in _column_names(schema16, table), table


def test_import_files_requires_user(schema16: duckdb.DuckDBPyConnection) -> None:
    """导入作业必须直接关联 user：不能只靠可能为空的目标账户确定归属。"""
    with pytest.raises(duckdb.ConstraintException):
        schema16.execute(
            """
            INSERT INTO import_files
                (id, file_hash, original_file_name, file_type, file_size_bytes,
                 parser_version, status, created_at, updated_at)
            VALUES ('11111111-2222-3333-4444-555555555555', repeat('a', 64), 'x.csv', 'csv',
                    1, 'v1', 'uploaded', current_timestamp, current_timestamp)
            """
        )


def test_ledger_versions_requires_user(schema16: duckdb.DuckDBPyConnection) -> None:
    """账本版本必须记录归属用户；当前用户版本由 WHERE user_id 定义。"""
    with pytest.raises(duckdb.ConstraintException):
        schema16.execute(
            "INSERT INTO ledger_versions (reason, source_reference) VALUES ('file_import', 'x')"
        )


def test_agent_session_identities_requires_user(schema16: duckdb.DuckDBPyConnection) -> None:
    """不可变会话身份必须记录 user，会话仓储据此校验归属。"""
    with pytest.raises(duckdb.ConstraintException):
        schema16.execute("INSERT INTO agent_session_identities (id) VALUES (uuid())")


def test_gmail_connections_requires_user(schema16: duckdb.DuckDBPyConnection) -> None:
    """Gmail 连接必须关联 user，且同一 Google subject 不再是全局唯一。"""
    with pytest.raises(duckdb.ConstraintException):
        schema16.execute(
            """
            INSERT INTO gmail_connections
                (id, google_subject, google_email, encrypted_refresh_token, granted_scopes,
                 status)
            VALUES (uuid(), 'sub', 'a@b.c', 'cipher', 'gmail.readonly', 'active')
            """
        )
    assert "UNIQUE(user_id, google_subject)" in _constraint_texts(schema16, "gmail_connections")


def test_market_source_settings_is_per_user(schema16: duckdb.DuckDBPyConnection) -> None:
    """行情设置从部署级单例改为按用户一行：singleton_key 必须消失。"""
    columns = _column_names(schema16, "market_source_settings")
    assert columns == {"user_id", "revision", "routes_json", "updated_at"}
    with pytest.raises(duckdb.ConstraintException):
        schema16.execute(
            "INSERT INTO market_source_settings (revision, routes_json, updated_at) "
            "VALUES (1, '{}'::JSON, current_timestamp)"
        )


def test_audit_events_separates_actor_and_subject_user(
    schema16: duckdb.DuckDBPyConnection,
) -> None:
    """业务审计区分操作者与资源所属用户，且部署级 system 事件允许都没有用户。"""
    columns = _column_names(schema16, "audit_events")
    assert {"actor_user_id", "subject_user_id"} <= columns
    schema16.execute(
        """
        INSERT INTO audit_events
            (id, occurred_at, event_type, outcome, actor_type, target_type, target_id,
             metadata_json)
        VALUES (uuid(), current_timestamp, 'system.startup', 'success', 'system', 'process',
                'lyra', '{}')
        """
    )
    row = schema16.execute(
        "SELECT actor_user_id, subject_user_id FROM audit_events"
    ).fetchone()
    assert row == (None, None)


def test_views_are_recreated_and_queryable(schema16: duckdb.DuckDBPyConnection) -> None:
    """四个视图必须随重建一起恢复，且定义仍可用。"""
    assert set(VIEW_NAMES) <= _view_names(schema16)
    for name in VIEW_NAMES:
        schema16.execute(f"SELECT * FROM {name} LIMIT 1").fetchall()


# =====================================================================
# 复合外键与用户内唯一：用真实越权写入撞约束
# =====================================================================


def test_same_legacy_account_key_allowed_across_users(
    schema16: duckdb.DuckDBPyConnection,
) -> None:
    """A/B 必须能各自拥有同键账户；同一用户重复则被拒绝。"""
    _insert_user(schema16, OWNER_USER_ID)
    _insert_user(schema16, OTHER_USER_ID, display_name="Other")
    _insert_account(schema16, OWNER_ACCOUNT_ID, OWNER_USER_ID, legacy_account_key="ACC-1")
    _insert_account(schema16, OTHER_ACCOUNT_ID, OTHER_USER_ID, legacy_account_key="ACC-1")

    with pytest.raises(duckdb.ConstraintException):
        _insert_account(
            schema16,
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaac",
            OWNER_USER_ID,
            legacy_account_key="ACC-1",
        )


def test_paired_device_rejects_identity_of_another_user(
    schema16: duckdb.DuckDBPyConnection,
) -> None:
    """复合外键 (user_id, identity_id) 必须拒绝跨用户组合。"""
    _insert_user(schema16, OWNER_USER_ID)
    _insert_user(schema16, OTHER_USER_ID, display_name="Other")
    _insert_identity(schema16, OTHER_IDENTITY_ID, OTHER_USER_ID, subject="other-subject")

    with pytest.raises(duckdb.ConstraintException):
        schema16.execute(
            """
            INSERT INTO paired_devices
                (id, user_id, identity_id, display_name, public_key_pem,
                 public_key_fingerprint, key_algorithm)
            VALUES (uuid(), CAST(? AS UUID), CAST(? AS UUID), 'Pixel', 'pem',
                    repeat('f', 64), 'ed25519')
            """,
            [OWNER_USER_ID, OTHER_IDENTITY_ID],
        )

    _insert_identity(schema16)
    schema16.execute(
        """
        INSERT INTO paired_devices
            (id, user_id, identity_id, display_name, public_key_pem,
             public_key_fingerprint, key_algorithm)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), CAST(? AS UUID), 'Pixel', 'pem',
                repeat('f', 64), 'ed25519')
        """,
        [OWNER_DEVICE_ID, OWNER_USER_ID, OWNER_IDENTITY_ID],
    )


def test_paired_device_rejects_cross_user_update(schema16: duckdb.DuckDBPyConnection) -> None:
    """复合外键在 UPDATE 上也生效：不能把设备改成借用他人的身份。"""
    _insert_user(schema16, OWNER_USER_ID)
    _insert_user(schema16, OTHER_USER_ID, display_name="Other")
    _insert_identity(schema16)
    _insert_identity(schema16, OTHER_IDENTITY_ID, OTHER_USER_ID, subject="other-subject")
    schema16.execute(
        """
        INSERT INTO paired_devices
            (id, user_id, identity_id, display_name, public_key_pem,
             public_key_fingerprint, key_algorithm)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), CAST(? AS UUID), 'Pixel', 'pem',
                repeat('f', 64), 'ed25519')
        """,
        [OWNER_DEVICE_ID, OWNER_USER_ID, OWNER_IDENTITY_ID],
    )

    with pytest.raises(duckdb.ConstraintException):
        schema16.execute(
            "UPDATE paired_devices SET identity_id = CAST(? AS UUID) WHERE id = CAST(? AS UUID)",
            [OTHER_IDENTITY_ID, OWNER_DEVICE_ID],
        )


def test_device_fingerprint_unique_within_user_only(
    schema16: duckdb.DuckDBPyConnection,
) -> None:
    """唯一键按 (user_id, 指纹) 划分：跨用户可同指纹，同用户重复被拒绝。"""
    _insert_user(schema16, OWNER_USER_ID)
    _insert_user(schema16, OTHER_USER_ID, display_name="Other")
    _insert_identity(schema16)
    _insert_identity(schema16, OTHER_IDENTITY_ID, OTHER_USER_ID, subject="other-subject")

    def _device(device_id: str, user_id: str, identity_id: str) -> None:
        schema16.execute(
            """
            INSERT INTO paired_devices
                (id, user_id, identity_id, display_name, public_key_pem,
                 public_key_fingerprint, key_algorithm)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), CAST(? AS UUID), 'Pixel', 'pem',
                    repeat('f', 64), 'ed25519')
            """,
            [device_id, user_id, identity_id],
        )

    _device(OWNER_DEVICE_ID, OWNER_USER_ID, OWNER_IDENTITY_ID)
    _device("dddddddd-dddd-dddd-dddd-ddddddddddd2", OTHER_USER_ID, OTHER_IDENTITY_ID)
    with pytest.raises(duckdb.ConstraintException):
        _device("dddddddd-dddd-dddd-dddd-ddddddddddd3", OWNER_USER_ID, OWNER_IDENTITY_ID)


def test_import_file_target_account_must_be_same_user(
    schema16: duckdb.DuckDBPyConnection,
) -> None:
    """导入作业的目标账户必须与作业同属一个用户；留空则放行。"""
    _insert_user(schema16, OWNER_USER_ID)
    _insert_user(schema16, OTHER_USER_ID, display_name="Other")
    _insert_account(schema16, OTHER_ACCOUNT_ID, OTHER_USER_ID)

    def _import_file(import_id: str, user_id: str, account_id: str | None) -> None:
        schema16.execute(
            """
            INSERT INTO import_files
                (id, user_id, file_hash, original_file_name, file_type, file_size_bytes,
                 parser_version, status, target_account_id, created_at, updated_at)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), repeat('a', 64), 'x.csv', 'csv', 1,
                    'v1', 'uploaded', CAST(? AS UUID), current_timestamp, current_timestamp)
            """,
            [import_id, user_id, account_id],
        )

    # 上传阶段还没有目标账户：可空列必须放行（MATCH SIMPLE）。
    _import_file(OWNER_IMPORT_ID, OWNER_USER_ID, None)
    with pytest.raises(duckdb.ConstraintException):
        _import_file("99999999-9999-9999-9999-999999999992", OWNER_USER_ID, OTHER_ACCOUNT_ID)


def test_gmail_subject_is_unique_per_user(schema16: duckdb.DuckDBPyConnection) -> None:
    """同一 Google subject 在不同用户下独立存储，不能被他人 upsert 顶掉。"""
    _insert_user(schema16, OWNER_USER_ID)
    _insert_user(schema16, OTHER_USER_ID, display_name="Other")

    def _connection(connection_id: str, user_id: str) -> None:
        schema16.execute(
            """
            INSERT INTO gmail_connections
                (id, user_id, google_subject, google_email, encrypted_refresh_token,
                 granted_scopes, status)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), 'google-subject-1', 'a@b.c',
                    'cipher', 'gmail.readonly', 'active')
            """,
            [connection_id, user_id],
        )

    _connection(OWNER_GMAIL_ID, OWNER_USER_ID)
    _connection("88888888-8888-8888-8888-888888888882", OTHER_USER_ID)
    with pytest.raises(duckdb.ConstraintException):
        _connection("88888888-8888-8888-8888-888888888883", OWNER_USER_ID)


def test_duplicate_identity_is_rejected(schema16: duckdb.DuckDBPyConnection) -> None:
    """身份键 (provider, subject) 全局唯一：邮箱相同不合并，主体相同才合并。"""
    _insert_user(schema16, OWNER_USER_ID)
    _insert_user(schema16, OTHER_USER_ID, display_name="Other")
    _insert_identity(schema16)

    with pytest.raises(duckdb.ConstraintException):
        _insert_identity(
            schema16, OTHER_IDENTITY_ID, OTHER_USER_ID, provider="google", subject="subject-1"
        )

    # 同一邮箱、不同 subject：必须允许成为另一条独立身份。
    _insert_identity(schema16, OTHER_IDENTITY_ID, OTHER_USER_ID, subject="subject-2")


# =====================================================================
# schema 15 → 16 升级路径
# =====================================================================


@pytest.mark.skipif(not FIXTURE_DATABASE.is_file(), reason="缺少冻结的既有账本夹具")
def test_upgrade_attributes_every_table_to_confirmed_owner(tmp_path: Path) -> None:
    """非空旧库带上下文升级后，全部归属写入、数据无损、视图可用。"""
    target = tmp_path / "upgrade.duckdb"
    _seed_schema15(target)
    con = duckdb.connect(str(target))
    try:
        owner_identity_before = con.execute(
            "SELECT provider, subject FROM server_owner_identity"
        ).fetchall()
        assert owner_identity_before, (
            "夹具的 schema 15 库里应当有旧 owner 身份记录——否则测不到删除行为"
        )
        apply_migrations(con, _owner_context())

        for table in ("accounts", "ledger_versions", "import_files", "paired_devices",
                      "gmail_connections", "agent_session_identities"):
            owners = {
                str(row[0])
                for row in con.execute(f"SELECT user_id FROM {table}").fetchall()
            }
            assert owners == {OWNER_USER_ID}, table
        row = con.execute("SELECT user_id FROM market_source_settings").fetchone()
        assert row is not None and str(row[0]) == OWNER_USER_ID

        # 金额与数量必须逐位保真（CTAS 保留 DECIMAL，回填不得引入浮点转换）。
        amount = con.execute("SELECT amount FROM transactions").fetchone()
        assert amount is not None and str(amount[0]) == "-1001.7345"
        pnl = con.execute("SELECT pnl FROM realized").fetchone()
        assert pnl is not None and str(pnl[0]) == "62.2593"
        quantity = con.execute("SELECT remaining_quantity FROM lots").fetchone()
        assert quantity is not None and str(quantity[0]) == "4.000000"

        # 逐表核对行数：重建不得丢行或多行。
        for table, expected in (
            ("accounts", 1), ("transactions", 2), ("lots", 1), ("realized", 1),
            ("ledger_version_accounts", 0), ("account_import_reminder_settings", 1),
            ("import_files", 1), ("paired_devices", 1), ("device_refresh_credentials", 1),
            ("device_access_tokens", 1), ("mutation_idempotency_records", 1),
            ("audit_events", 1), ("agent_session_identities", 1), ("agent_sessions", 1),
            ("agent_messages", 1), ("agent_tool_calls", 1), ("gmail_connections", 1),
            ("gmail_account_mappings", 1), ("gmail_messages", 1),
            ("gmail_message_transactions", 1), ("market_source_settings", 1),
            ("users", 1), ("user_identities", 2),
        ):
            assert _count(con, table) == expected, table

        # 旧设备没有登录身份记录，必须挂到上下文里显式指定的那一条身份上。
        device_identity = con.execute(
            """
            SELECT user_identities.provider, user_identities.subject
            FROM paired_devices
            JOIN user_identities ON user_identities.id = paired_devices.identity_id
            """
        ).fetchone()
        assert device_identity == ("google", "112233")

        # 审计事件按契约回填：资源所属恒为原用户，操作者仅在设备事件上回填。
        audit = con.execute(
            "SELECT actor_user_id, subject_user_id FROM audit_events"
        ).fetchone()
        assert audit is not None
        assert str(audit[0]) == OWNER_USER_ID and str(audit[1]) == OWNER_USER_ID

        # 旧 owner 身份表必须被 v16 删除：迁移后它的记录已经落到 user_identities 上，
        # 留着它等于留下第二条可读的授权来源。**删除必须发生在迁移里**，否则直接
        # `open_lyra_database` 建出来的新库会留着这张空表，两条路径表集合不一致。
        legacy_present = {
            str(row[0])
            for row in con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE table_name = 'server_owner_identity'"
            ).fetchall()
        }
        assert legacy_present == set(), "v16 未删除旧 owner 身份表"

        for name in VIEW_NAMES:
            con.execute(f"SELECT * FROM {name} LIMIT 1").fetchall()
        leftovers = {
            str(row[0])
            for row in con.execute(
                "SELECT table_name FROM duckdb_tables() "
                "WHERE table_name LIKE '_stage%' OR table_name LIKE '_migration%'"
            ).fetchall()
        }
        assert leftovers == set()
    finally:
        con.close()


@pytest.mark.skipif(not FIXTURE_DATABASE.is_file(), reason="缺少冻结的既有账本夹具")
def test_upgrade_without_owner_context_fails_loudly(tmp_path: Path) -> None:
    """非空旧库没有显式归属时必须**明确失败**，绝不分配给默认用户或首次登录者。"""
    target = tmp_path / "no-context.duckdb"
    _seed_schema15(target)
    con = duckdb.connect(str(target))
    try:
        with pytest.raises(MigrationError, match="必须显式提供原 owner 归属确认"):
            apply_migrations(con)
        # 失败后旧库必须仍然可用且未被动过。
        assert _count(con, "accounts") == 1
        assert "user_id" not in _column_names(con, "accounts")
        assert _count(con, "schema_migrations") == 15
        assert "users" not in _table_names(con)
    finally:
        con.close()


def test_empty_database_needs_no_context(tmp_path: Path) -> None:
    """全新空库不需要上下文即可迁移：这是删掉默认 owner 之后的正常冷启动。"""
    database = open_lyra_database(str(tmp_path / "empty.duckdb"))
    try:
        assert database.read_health()["schemaVersion"] == CURRENT_SCHEMA_VERSION
        assert _count(database.connection, "users") == 0
    finally:
        database.close()


@pytest.mark.skipif(not FIXTURE_DATABASE.is_file(), reason="缺少冻结的既有账本夹具")
def test_upgrade_requires_exactly_one_device_identity_when_devices_exist(
    tmp_path: Path,
) -> None:
    """旧库有设备却没指定设备身份来源时必须失败，而不是随便挑一条身份。"""
    target = tmp_path / "no-device-identity.duckdb"
    _seed_schema15(target)
    con = duckdb.connect(str(target))
    try:
        context = _owner_context(
            identities=(
                MigrationOwnerIdentity(provider="google", subject="112233"),
                MigrationOwnerIdentity(provider="github", subject="445566"),
            )
        )
        with pytest.raises(MigrationError, match="只能指定一条 is_device_identity"):
            apply_migrations(con, context)
        assert "users" not in _table_names(con)
    finally:
        con.close()


@pytest.mark.skipif(not FIXTURE_DATABASE.is_file(), reason="缺少冻结的既有账本夹具")
def test_upgrade_rejects_duplicate_identities_in_context(tmp_path: Path) -> None:
    """上下文自身重复的身份是操作者的输入错误，必须在动数据之前失败。"""
    target = tmp_path / "dup-identity.duckdb"
    _seed_schema15(target)
    con = duckdb.connect(str(target))
    try:
        context = _owner_context(
            identities=(
                MigrationOwnerIdentity(
                    provider="google", subject="112233", is_device_identity=True
                ),
                MigrationOwnerIdentity(provider="google", subject="112233"),
            )
        )
        with pytest.raises(MigrationError, match="重复身份"):
            apply_migrations(con, context)
        assert "users" not in _table_names(con)
    finally:
        con.close()


def test_upgrade_rejects_malformed_owner_user_id(tmp_path: Path) -> None:
    """owner_user_id 必须是合法 UUID；否则失败，而不是写进库再炸。"""
    target = tmp_path / "bad-uuid.duckdb"
    _seed_schema15(target)
    con = duckdb.connect(str(target))
    try:
        with pytest.raises(MigrationError, match="不是合法 UUID"):
            apply_migrations(con, _owner_context(owner_user_id="not-a-uuid"))
        assert "users" not in _table_names(con)
    finally:
        con.close()


@pytest.mark.skipif(not FIXTURE_DATABASE.is_file(), reason="缺少冻结的既有账本夹具")
def test_failed_upgrade_rolls_back_to_working_schema15(tmp_path: Path) -> None:
    """升级中途失败必须整体回滚：旧表、旧数据、旧视图都还能用，且无暂存残留。

    故障注入方式：在旧库里放一张迁移不认识的、引用 ``accounts`` 的表。它在阶段 2
    会让 ``DROP TABLE accounts`` 撞上「被引用父表不能删除」，属于真实的迁移中途失败，
    而不是在入口处被校验挡下。
    """
    target = tmp_path / "rollback.duckdb"
    _seed_schema15(target)
    writer = duckdb.connect(str(target))
    try:
        writer.execute(
            "CREATE TABLE zz_unknown_child (account_id UUID REFERENCES accounts(id))"
        )
    finally:
        writer.close()

    con = duckdb.connect(str(target))
    try:
        with pytest.raises(duckdb.CatalogException):
            apply_migrations(con, _owner_context())

        assert _count(con, "accounts") == 1
        assert _count(con, "transactions") == 2
        assert _count(con, "schema_migrations") == 15
        assert "users" not in _table_names(con)
        for name in VIEW_NAMES:
            con.execute(f"SELECT * FROM {name} LIMIT 1").fetchall()
        leftovers = {
            str(row[0])
            for row in con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE table_name LIKE '_stage%'"
            ).fetchall()
        }
        assert leftovers == set()
    finally:
        con.close()


@pytest.mark.skipif(not FIXTURE_DATABASE.is_file(), reason="缺少冻结的既有账本夹具")
def test_restart_does_not_remigrate_or_recreate_users(tmp_path: Path) -> None:
    """重启不得重复迁移，也不得重新生成用户：第二次调用是纯校验。"""
    target = tmp_path / "restart.duckdb"
    _seed_schema15(target)
    con = duckdb.connect(str(target))
    try:
        apply_migrations(con, _owner_context())
        first = con.execute("SELECT id, created_at FROM users").fetchall()
        apply_migrations(con, _owner_context())
        assert con.execute("SELECT id, created_at FROM users").fetchall() == first
        assert _count(con, "user_identities") == 2
    finally:
        con.close()


@pytest.mark.skipif(not FIXTURE_DATABASE.is_file(), reason="缺少冻结的既有账本夹具")
def test_ledger_version_sequence_continues_after_upgrade(tmp_path: Path) -> None:
    """重建后序列必须仍然严格领先于已装载的版本号，否则下一次写入会撞主键。"""
    target = tmp_path / "sequence.duckdb"
    _seed_schema15(target)
    con = duckdb.connect(str(target))
    try:
        apply_migrations(con, _owner_context())
        highest = con.execute("SELECT max(version) FROM ledger_versions").fetchone()
        assert highest is not None
        con.execute(
            "INSERT INTO ledger_versions (reason, source_reference, user_id) "
            "VALUES ('file_import', 'next', CAST(? AS UUID))",
            [OWNER_USER_ID],
        )
        next_version = con.execute("SELECT max(version) FROM ledger_versions").fetchone()
        assert next_version is not None and int(next_version[0]) > int(highest[0])
    finally:
        con.close()


ACTIVE_REFRESH_ID = "4d4d4d4d-4d4d-4d4d-4d4d-4d4d4d4d4d4d"
ACTIVE_ACCESS_ID = "6f6f6f6f-6f6f-6f6f-6f6f-6f6f6f6f6f6f"


@pytest.mark.skipif(not FIXTURE_DATABASE.is_file(), reason="缺少冻结的既有账本夹具")
def test_upgrade_revokes_pre_existing_device_credentials(tmp_path: Path) -> None:
    """迁移必须撤销**旧设备凭据**（计划 §1.8「发布包含数据迁移和重新登录」、§9.4）。

    schema 16 引入了授权模型（用户 → 设备），而 schema 15 的凭据是在**没有用户概念**
    时签发的。让它们继续有效，等于让前授权模型时期的会话悄悄穿过一次授权模型的引入。

    `_seed_schema15` 里的种子凭据是 ``rotated`` / ``revoked``，**测不到这条规则**，
    因此这里显式追加一对 ``active`` 凭据。

    **只动状态、不删行**：设备与凭据的历史要保留，重放检测与审计都需要它们。
    """
    target = tmp_path / "revoke-credentials.duckdb"
    _seed_schema15(target)
    con = duckdb.connect(str(target))
    try:
        con.execute(
            """
            INSERT INTO device_refresh_credentials
                (id, device_id, family_id, generation, token_hash, status, expires_at,
                 created_at)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), CAST(? AS UUID), 1,
                    repeat('3', 64), 'active', current_timestamp + INTERVAL 30 DAY,
                    current_timestamp)
            """,
            [ACTIVE_REFRESH_ID, OWNER_DEVICE_ID, "5e5e5e5e-5e5e-5e5e-5e5e-5e5e5e5e5e5e"],
        )
        con.execute(
            """
            INSERT INTO device_access_tokens
                (id, device_id, token_hash, status, expires_at, issued_at)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), repeat('4', 64), 'active',
                    current_timestamp + INTERVAL 15 MINUTE, current_timestamp)
            """,
            [ACTIVE_ACCESS_ID, OWNER_DEVICE_ID],
        )

        apply_migrations(con, _owner_context())

        refresh = con.execute(
            "SELECT status, revoked_at IS NOT NULL FROM device_refresh_credentials "
            "WHERE id = CAST(? AS UUID)",
            [ACTIVE_REFRESH_ID],
        ).fetchone()
        access = con.execute(
            "SELECT status, revoked_at IS NOT NULL FROM device_access_tokens "
            "WHERE id = CAST(? AS UUID)",
            [ACTIVE_ACCESS_ID],
        ).fetchone()
        assert refresh == ("revoked", True), "旧刷新凭据必须被撤销"
        assert access == ("revoked", True), "旧访问令牌必须被撤销"

        # 行数不变：撤销是状态变更，不是删除。
        assert con.execute(
            "SELECT count(*) FROM device_refresh_credentials"
        ).fetchone() == (2,)
        assert con.execute("SELECT count(*) FROM device_access_tokens").fetchone() == (2,)

        # 已撤销的行保留原撤销时间，不被改写成今天。
        untouched = con.execute(
            "SELECT revoked_at FROM device_access_tokens WHERE id = CAST(? AS UUID)",
            ["3c3c3c3c-3c3c-3c3c-3c3c-3c3c3c3c3c3c"],
        ).fetchone()
        assert untouched is not None and untouched[0] is not None
    finally:
        con.close()
