"""Agent 会话仓储测试（真实 DuckDB）。

最要紧的一条是 :func:`test_uuid_scope_does_not_match_legacy_key`：
会话里的账户是 **UUID**，账本里的是**脱敏键**。把 UUID 当账户键去读账本**不会报错**，
只会返回空账本——这类 bug 只能靠显式断言拦住。

第二条是归属：``agent_sessions`` 自己不带 ``user_id``，归属记在不可变的
``agent_session_identities`` 上。因此这里必须验证"按用户读"的查询——不属于该用户的
会话与不存在的会话返回同一个 ``None``（V07）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lyra.application.app_error import ApplicationError
from lyra.db.connection import open_lyra_database
from lyra.domain.agent.switching import AgentProviderAvailability, create_agent_session
from lyra.domain.analytics.types import AccountScope
from lyra.infrastructure.db.agent_session_repository import DuckDbAgentSessionRepository
from lyra.infrastructure.db.ledger_repository import DuckDbLedgerRepository

ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ACCOUNT_ID = "44444444-4444-4444-4444-444444444444"
DEVICE_ID = "22222222-2222-2222-2222-222222222222"
OTHER_DEVICE_ID = "55555555-5555-5555-5555-555555555555"
SESSION_ID = "33333333-3333-3333-3333-333333333333"
USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
LEGACY_KEY = "TST001"
NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)


def _available() -> AgentProviderAvailability:
    return AgentProviderAvailability(provider="genkit", status="available")


@pytest.fixture()
def wired(tmp_path: Path):
    """装配真实库、两个用户各自的账户与设备，以及会话仓储。"""
    database = open_lyra_database(str(tmp_path / "agent.duckdb"))
    connection = database.connection
    for user_id, display_name in ((USER_ID, "Owner"), (OTHER_USER_ID, "Other")):
        connection.execute(
            "INSERT INTO users (id, display_name) VALUES (CAST(? AS UUID), ?)",
            [user_id, display_name],
        )
    connection.execute(
        """
        INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Account', ?, '001')
        """,
        [ACCOUNT_ID, USER_ID, LEGACY_KEY],
    )
    # 另一个用户的账户用**同一个** legacy key：账户键已改为用户内唯一。
    connection.execute(
        """
        INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Other', ?, '002')
        """,
        [OTHER_ACCOUNT_ID, OTHER_USER_ID, LEGACY_KEY],
    )
    # agent_sessions.device_id 有指向 paired_devices 的外键，paired_devices 又有
    # 指向 users / user_identities 的外键，因此设备需要身份行。
    for user_id, device_id, fingerprint in (
        (USER_ID, DEVICE_ID, "f" * 64),
        (OTHER_USER_ID, OTHER_DEVICE_ID, "e" * 64),
    ):
        identity_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO user_identities (id, user_id, provider, subject)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), 'github', ?)
            """,
            [identity_id, user_id, f"subject-{device_id}"],
        )
        connection.execute(
            """
            INSERT INTO paired_devices
                (id, user_id, identity_id, display_name, public_key_pem,
                 public_key_fingerprint, key_algorithm, status)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), CAST(? AS UUID), 'Pixel 9',
                    '-----BEGIN PUBLIC KEY-----', ?, 'ec', 'active')
            """,
            [device_id, user_id, identity_id, fingerprint],
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
    repository.create_session(_session(), device_id=DEVICE_ID, user_id=USER_ID, now=NOW)

    row = repository.load_session(SESSION_ID)

    assert row is not None
    assert row.id == SESSION_ID
    assert row.device_id == DEVICE_ID
    # 归属来自不可变的会话身份表，而不是 ``agent_sessions``（它是可变表）。
    assert row.user_id == USER_ID
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
    repository.create_session(_session(), device_id=DEVICE_ID, user_id=USER_ID, now=NOW)

    row = repository.load_session(SESSION_ID)
    assert row is not None
    assert row.original_provider is None


def test_account_scope_round_trips_as_uuid(wired) -> None:
    """账户范围里的 ``account`` 是 **UUID**，不是脱敏键。"""
    repository, _ = wired
    repository.create_session(_session(), device_id=DEVICE_ID, user_id=USER_ID, now=NOW)

    row = repository.load_session(SESSION_ID)

    assert row is not None
    assert row.account_scope.kind == "account"
    assert row.account_scope.account == ACCOUNT_ID


def test_all_scope_stores_null_account(wired) -> None:
    """``all`` 范围必须存 NULL——schema 的 CHECK 约束要求两者一致。"""
    repository, connection = wired
    repository.create_session(
        _session(AccountScope(kind="all")), device_id=DEVICE_ID, user_id=USER_ID, now=NOW
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
    repository.create_session(_session(), device_id=DEVICE_ID, user_id=USER_ID, now=NOW)

    count = connection.execute("SELECT count(*) FROM agent_session_identities").fetchone()
    assert count is not None and int(count[0]) == 1
    # 归属只写在身份表上：会话表不带 user_id（它是可变表）。
    owner = connection.execute(
        "SELECT user_id FROM agent_session_identities WHERE id = CAST(? AS UUID)",
        [SESSION_ID],
    ).fetchone()
    assert owner is not None and str(owner[0]) == USER_ID


def test_load_unknown_session_returns_none(wired) -> None:
    """不存在的会话返回 None。"""
    repository, _ = wired
    assert repository.load_session("99999999-9999-9999-9999-999999999999") is None


# --------------------------------------------------------------------------
# 归属
# --------------------------------------------------------------------------


def test_load_session_for_user_returns_the_owners_session(wired) -> None:
    """本用户的会话按「用户 + ID」读得到；同一用户另一台设备也读得到（V08）。"""
    repository, _ = wired
    repository.create_session(_session(), device_id=DEVICE_ID, user_id=USER_ID, now=NOW)

    row = repository.load_session_for_user(SESSION_ID, USER_ID)

    assert row is not None and row.user_id == USER_ID


def test_load_session_for_user_hides_another_users_session(wired) -> None:
    """**不属于当前用户的会话与不存在的会话返回同一个 None**（V07）。

    两种情况的返回值与错误形状必须一致，否则错误码就成了"他人会话是否存在"的探针。
    """
    repository, _ = wired
    repository.create_session(_session(), device_id=DEVICE_ID, user_id=USER_ID, now=NOW)

    assert repository.load_session_for_user(SESSION_ID, OTHER_USER_ID) is None
    assert repository.load_session_for_user(
        "99999999-9999-9999-9999-999999999999", OTHER_USER_ID
    ) is None


def test_create_session_rejects_an_account_of_another_user(wired) -> None:
    """**创建设备与账户必须同属该用户**：数据库的单列外键保证不了这一点。"""
    repository, connection = wired

    with pytest.raises(ApplicationError) as raised:
        repository.create_session(
            _session(AccountScope(kind="account", account=OTHER_ACCOUNT_ID)),
            device_id=DEVICE_ID,
            user_id=USER_ID,
            now=NOW,
        )

    assert raised.value.code == "AGENT_SESSION_SCOPE_INVALID"
    # 拒绝时不得留下任何半成品（身份行也没有）。
    count = connection.execute("SELECT count(*) FROM agent_session_identities").fetchone()
    assert count is not None and int(count[0]) == 0


def test_create_session_rejects_a_device_of_another_user(wired) -> None:
    """设备与用户不一致时同样拒绝，而不是先落库再在别处失败。"""
    repository, _ = wired

    with pytest.raises(ApplicationError) as raised:
        repository.create_session(
            _session(), device_id=OTHER_DEVICE_ID, user_id=USER_ID, now=NOW
        )

    assert raised.value.code == "AGENT_SESSION_SCOPE_INVALID"


# --------------------------------------------------------------------------
# 语义分叉的显式防护
# --------------------------------------------------------------------------


def test_resolves_uuid_to_legacy_key(wired) -> None:
    """会话里的 UUID 必须能解析成账本用的脱敏键。"""
    repository, _ = wired
    assert repository.resolve_legacy_account_key(USER_ID, ACCOUNT_ID) == LEGACY_KEY


def test_resolve_unknown_account_returns_none(wired) -> None:
    """未知账户返回 None，调用方据此报错而不是继续查空。"""
    repository, _ = wired
    assert repository.resolve_legacy_account_key(
        USER_ID, "99999999-9999-9999-9999-999999999999"
    ) is None


def test_another_users_account_uuid_resolves_to_none(wired) -> None:
    """**解析带用户范围**：B 的账户 UUID 在 A 名下解析为 None（P0 契约 §3.11）。

    否则 A 传一个别人的账户 UUID 就能拿到对方的脱敏键，随后账本查询会带上它。
    """
    repository, _ = wired
    assert repository.resolve_legacy_account_key(USER_ID, OTHER_ACCOUNT_ID) is None
    assert (
        repository.resolve_legacy_account_key(OTHER_USER_ID, OTHER_ACCOUNT_ID) == LEGACY_KEY
    )


def test_uuid_scope_does_not_match_legacy_key(wired) -> None:
    """**把 UUID 当账户键去读账本会静默得到空账本。**

    这条测试把那种失败模式固定下来：它不是异常，只是空结果。因此任何拿会话范围
    读账本的地方都必须先经 :meth:`resolve_legacy_account_key`。
    """
    repository, connection = wired
    connection.execute(
        """
        INSERT INTO ledger_versions (user_id, reason, source_reference)
        VALUES (CAST(? AS UUID), 'file_import', 'seed')
        """,
        [USER_ID],
    )
    repository.create_session(_session(), device_id=DEVICE_ID, user_id=USER_ID, now=NOW)

    ledger = DuckDbLedgerRepository(connection, user_id=USER_ID)
    # 直接用 UUID 当账户键：静默查不到任何交易。
    wrong = ledger.load_snapshot(AccountScope(kind="account", account=ACCOUNT_ID))
    assert wrong.transactions == ()
    assert wrong.accounts == (), "用 UUID 竟然匹配到了账户"

    # 经解析成脱敏键后才有账户。
    key = repository.resolve_legacy_account_key(USER_ID, ACCOUNT_ID)
    assert key is not None
    right = ledger.load_snapshot(AccountScope(kind="account", account=key))
    assert len(right.accounts) == 1
