"""Agent 会话仓储测试（真实 DuckDB）。

最要紧的一条是 :func:`test_uuid_scope_does_not_match_legacy_key`：
会话里的账户是 **UUID**，账本里的是**脱敏键**。把 UUID 当账户键去读账本**不会报错**，
只会返回空账本——这类 bug 只能靠显式断言拦住。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from lyra.db.connection import open_lyra_database
from lyra.domain.agent.switching import AgentProviderAvailability, create_agent_session
from lyra.domain.analytics.types import AccountScope
from lyra.infrastructure.db.agent_session_repository import DuckDbAgentSessionRepository
from lyra.infrastructure.db.ledger_repository import DuckDbLedgerRepository

ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"
DEVICE_ID = "22222222-2222-2222-2222-222222222222"
SESSION_ID = "33333333-3333-3333-3333-333333333333"
LEGACY_KEY = "TST001"
NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)


def _available() -> AgentProviderAvailability:
    return AgentProviderAvailability(provider="genkit", status="available")


@pytest.fixture()
def wired(tmp_path: Path):
    """装配真实库、账户与会话仓储。"""
    database = open_lyra_database(str(tmp_path / "agent.duckdb"))
    connection = database.connection
    connection.execute(
        """
        INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), 'Account', ?, '001')
        """,
        [ACCOUNT_ID, LEGACY_KEY],
    )
    # agent_sessions.device_id 有指向 paired_devices 的外键。
    connection.execute(
        """
        INSERT INTO paired_devices
            (id, display_name, public_key_pem, public_key_fingerprint, key_algorithm, status)
        VALUES (CAST(? AS UUID), 'Pixel 9', '-----BEGIN PUBLIC KEY-----', ?, 'ec', 'active')
        """,
        [DEVICE_ID, "f" * 64],
    )
    try:
        yield DuckDbAgentSessionRepository(connection), connection
    finally:
        database.close()


def _session(scope: AccountScope | None = None):
    """构造一个会话快照；账户范围默认用 **UUID**。"""
    return create_agent_session(
        session_id=SESSION_ID,
        provider="genkit",
        availability=_available(),
        account_scope=scope or AccountScope(kind="account", account=ACCOUNT_ID),
        ledger_version=3,
        as_of="2026-10-12T09:00:00.000Z",
    )


# --------------------------------------------------------------------------
# 创建与读取
# --------------------------------------------------------------------------


def test_create_and_load_round_trip(wired) -> None:
    """创建后读回必须逐字段一致，时间戳为 ISO 文本。"""
    repository, _ = wired
    repository.create_session(_session(), device_id=DEVICE_ID, now=NOW)

    row = repository.load_session(SESSION_ID)

    assert row is not None
    assert row.id == SESSION_ID
    assert row.device_id == DEVICE_ID
    assert row.provider == "genkit"
    assert row.status == "idle"
    assert row.ledger_version == 3
    assert row.revision == 1
    assert row.last_event_sequence == 0
    assert row.summary is None
    assert row.as_of == "2026-10-12T09:00:00.000Z"
    assert row.created_at == "2026-10-12T09:00:00.000Z"
    assert row.closed_at is None


def test_new_session_has_no_original_provider(wired) -> None:
    """``original_provider`` 是**迁移标记**，新会话必须为 NULL。

    它的 CHECK 只允许旧供应商（claude / pi / opencode）——把 "genkit" 写进去会被
    数据库拒绝。因此"原供应商"这个直觉命名是错的。
    """
    repository, _ = wired
    repository.create_session(_session(), device_id=DEVICE_ID, now=NOW)

    row = repository.load_session(SESSION_ID)
    assert row is not None
    assert row.original_provider is None


def test_account_scope_round_trips_as_uuid(wired) -> None:
    """账户范围里的 ``account`` 是 **UUID**，不是脱敏键。"""
    repository, _ = wired
    repository.create_session(_session(), device_id=DEVICE_ID, now=NOW)

    row = repository.load_session(SESSION_ID)

    assert row is not None
    assert row.account_scope.kind == "account"
    assert row.account_scope.account == ACCOUNT_ID


def test_all_scope_stores_null_account(wired) -> None:
    """``all`` 范围必须存 NULL——schema 的 CHECK 约束要求两者一致。"""
    repository, connection = wired
    repository.create_session(
        _session(AccountScope(kind="all")), device_id=DEVICE_ID, now=NOW
    )

    stored = connection.execute(
        "SELECT account_scope_kind, account_id FROM agent_sessions"
    ).fetchone()
    assert stored is not None
    assert stored[0] == "all"
    assert stored[1] is None

    row = repository.load_session(SESSION_ID)
    assert row is not None and row.account_scope.kind == "all"


def test_session_identity_is_written_on_create(wired) -> None:
    """身份表必须创建时就写——它是会话表被重建时的身份来源，事后无法补。"""
    repository, connection = wired
    repository.create_session(_session(), device_id=DEVICE_ID, now=NOW)

    count = connection.execute("SELECT count(*) FROM agent_session_identities").fetchone()
    assert count is not None and int(count[0]) == 1


def test_load_unknown_session_returns_none(wired) -> None:
    """不存在的会话返回 None。"""
    repository, _ = wired
    assert repository.load_session("99999999-9999-9999-9999-999999999999") is None


# --------------------------------------------------------------------------
# 语义分叉的显式防护
# --------------------------------------------------------------------------


def test_resolves_uuid_to_legacy_key(wired) -> None:
    """会话里的 UUID 必须能解析成账本用的脱敏键。"""
    repository, _ = wired
    assert repository.resolve_legacy_account_key(ACCOUNT_ID) == LEGACY_KEY


def test_resolve_unknown_account_returns_none(wired) -> None:
    """未知账户返回 None，调用方据此报错而不是继续查空。"""
    repository, _ = wired
    assert repository.resolve_legacy_account_key(
        "99999999-9999-9999-9999-999999999999"
    ) is None


def test_uuid_scope_does_not_match_legacy_key(wired) -> None:
    """**把 UUID 当账户键去读账本会静默得到空账本。**

    这条测试把那种失败模式固定下来：它不是异常，只是空结果。因此任何拿会话范围
    读账本的地方都必须先经 :meth:`resolve_legacy_account_key`。
    """
    repository, connection = wired
    connection.execute(
        """
        INSERT INTO ledger_versions (reason, source_reference) VALUES ('file_import', 'seed')
        """
    )
    repository.create_session(_session(), device_id=DEVICE_ID, now=NOW)

    ledger = DuckDbLedgerRepository(connection)
    # 直接用 UUID 当账户键：静默查不到任何交易。
    wrong = ledger.load_snapshot(AccountScope(kind="account", account=ACCOUNT_ID))
    assert wrong.transactions == ()
    assert wrong.accounts == (), "用 UUID 竟然匹配到了账户"

    # 经解析成脱敏键后才有账户。
    key = repository.resolve_legacy_account_key(ACCOUNT_ID)
    assert key is not None
    right = ledger.load_snapshot(AccountScope(kind="account", account=key))
    assert len(right.accounts) == 1
