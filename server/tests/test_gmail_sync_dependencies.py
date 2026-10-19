"""Gmail 同步依赖在**每一步**都拒绝已停用的用户（计划 §4.4、V11 的 Gmail 侧）。

已有的端到端停用用例（`test_gmail_routes.py::test_real_service_refuses_to_sync_after_the_user_is_disabled`）
只覆盖到 ``load_connection``——那是同步的第一步。``refresh_access_token`` /
``fetch_incremental`` / ``fetch_initial`` / ``save_connection`` 上的检查**不会被它命中**，
于是"删掉其中任意一个"都不会有测试变红。

这些检查防的是一次**真实竞态**而不是死代码：``load_connection`` 通过之后、刷新令牌
之前用户被停用（停用与业务写入经同一把单写锁排序，但在飞请求已经在流程中）。
闸门必须在每个真正触达外部或落库的动作之前各自再问一次。

这里直接构造同步依赖而不是发 HTTP 请求，因为要验证的正是"请求早已通过入队检查"
之后的时序——``require_user`` 拦不住这种在飞请求。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from lyra.application.app_error import ApplicationError
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.db.gmail_connection_repository import (
    create_gmail_account_mapping_row,
    load_gmail_account_mappings,
)
from lyra.infrastructure.gmail.service_factory import _Dependencies, _SyncDependencies
from lyra.security.secret_cipher import SecretCipher, master_key_from_hex

USER_ID = "11111111-1111-4111-8111-111111111111"
CONNECTION_ID = "22222222-2222-4222-8222-222222222222"
STAMP = "2026-10-16T09:00:00.000Z"
ACCOUNT_ID = "33333333-3333-4333-8333-333333333333"
OTHER_ACCOUNT_ID = "44444444-4444-4444-8444-444444444444"
OTHER_USER_ID = "55555555-5555-4555-8555-555555555555"
OTHER_USERS_ACCOUNT_ID = "66666666-6666-4666-8666-666666666666"


@pytest.fixture()
def wired(tmp_path: Path):
    """一个活动用户 + 一条属于他的 Gmail 连接，以及真实的同步依赖。"""
    database = open_lyra_database(str(tmp_path / "gmail-sync.duckdb"))
    connection = database.connection
    connection.execute(
        "INSERT INTO users (id, display_name, status, role) "
        "VALUES (CAST(? AS UUID), 'Owner', 'active', 'member')",
        [USER_ID],
    )
    connection.execute(
        """
        INSERT INTO gmail_connections (
            id, user_id, google_subject, google_email, encrypted_refresh_token,
            granted_scopes, status
        ) VALUES (
            CAST(? AS UUID), CAST(? AS UUID), 'sync-subject', 'owner@example.com',
            'v1.iv.ct.tag', 'scope', 'pending_initial_scan'
        )
        """,
        [CONNECTION_ID, USER_ID],
    )
    dependencies = _SyncDependencies(
        connection=connection,
        cipher=SecretCipher(master_key_from_hex("0" * 64)),
        maximum_messages=10,
        user_id=USER_ID,
    )
    try:
        yield connection, dependencies
    finally:
        database.close()


def _disable(connection) -> None:
    """把用户置为停用。"""
    connection.execute(
        "UPDATE users SET status = 'disabled', updated_at = current_timestamp "
        "WHERE id = CAST(? AS UUID)",
        [USER_ID],
    )


def _assert_rejected(action, label: str) -> None:
    """断言该动作以 ``USER_DISABLED`` 被拒（403、不可重试）。"""
    with pytest.raises(ApplicationError) as raised:
        action()
    assert raised.value.code == "USER_DISABLED", f"{label} 应以 USER_DISABLED 拒绝"
    assert raised.value.status_code == 403
    assert raised.value.retryable is False


def test_a_disabled_user_is_rejected_at_every_sync_step(wired) -> None:
    """**四个外部/落库动作逐一验证**，而不只验证第一步。

    每个动作都在任何 I/O 之前先查活动状态，因此这里不需要网络替身：检查一旦缺失，
    断言会以"真的去访问 Google"的形式失败，而不是静默通过。
    """
    _connection, dependencies = wired

    # 对照：用户活动时第一步确实能读到连接——否则下面的 403 可能来自别的原因。
    row = dependencies.load_connection(CONNECTION_ID)
    assert row is not None and row.google_subject == "sync-subject"
    expected_updated_at = row.updated_at

    _disable(_connection)

    _assert_rejected(lambda: dependencies.load_connection(CONNECTION_ID), "load_connection")
    _assert_rejected(lambda: dependencies.refresh_access_token(row), "refresh_access_token")
    _assert_rejected(
        lambda: dependencies.fetch_incremental("token", "100", 10), "fetch_incremental"
    )
    _assert_rejected(
        lambda: dependencies.fetch_initial("token", "01/01/2026", 10), "fetch_initial"
    )
    _assert_rejected(
        lambda: dependencies.save_connection(
            row, expected_updated_at=expected_updated_at
        ),
        "save_connection",
    )


def test_nothing_is_written_back_when_the_user_is_disabled(wired) -> None:
    """停用后 ``save_connection`` 不写回：**结果属性**，不是第一道防线的隔离验证。

    "不写回"有**两道**防线：先是活动检查直接 403，其次是 ``save_gmail_connection_row``
    的 ``updated_at`` CAS。因此本用例即使移除活动检查也仍然通过——它证明的是最终结果，
    而不是某一道防线。**防线本身由上一个用例逐点验证**（已做反向确认：移除检查后那条
    会变红）。

    保留它的价值在于：真正的后果是游标与 ``last_sync_at`` 不能被在飞同步写回，而
    "抛了 403"并不自动蕴含这一点。
    """
    connection, dependencies = wired
    row = dependencies.load_connection(CONNECTION_ID)
    assert row is not None
    before = connection.execute(
        "SELECT status, last_sync_at, history_id FROM gmail_connections "
        "WHERE id = CAST(? AS UUID)",
        [CONNECTION_ID],
    ).fetchone()

    _disable(connection)

    with pytest.raises(ApplicationError):
        dependencies.save_connection(
            row, expected_updated_at=row.updated_at, history_id="999"
        )

    after = connection.execute(
        "SELECT status, last_sync_at, history_id FROM gmail_connections "
        "WHERE id = CAST(? AS UUID)",
        [CONNECTION_ID],
    ).fetchone()
    assert after == before


def test_the_guard_covers_a_user_that_never_existed(wired) -> None:
    """用户不存在与已停用**同一种拒绝**——不区分只会给调用方多一条无意义分支。"""
    _connection, dependencies = wired
    orphan = _SyncDependencies(
        connection=_connection,
        cipher=SecretCipher(master_key_from_hex("0" * 64)),
        maximum_messages=10,
        user_id=str(uuid.uuid4()),
    )

    _assert_rejected(lambda: orphan.load_connection(CONNECTION_ID), "load_connection")


def test_full_initial_scan_omits_date_filter(wired) -> None:
    """显式全量首次扫描只查询嘉信发件人与主题，不伪造日期下界。"""
    _connection, dependencies = wired
    row = dependencies.load_connection(CONNECTION_ID)
    assert row is not None
    requested_urls: list[str] = []

    def get_json(url, _headers, _timeout):
        requested_urls.append(url)
        if url.endswith("/profile"):
            return 200, {
                "emailAddress": "owner@example.com",
                "historyId": "100",
                "messagesTotal": 0,
                "threadsTotal": 0,
            }
        return 200, {"messages": []}

    dependencies._get_json = get_json
    fetched = dependencies.fetch_initial("token", None, 10)

    assert fetched.kind == "ready"
    assert len(requested_urls) == 2
    query = parse_qs(urlparse(requested_urls[1]).query)["q"][0]
    assert query.startswith("from:")
    assert 'subject:"Schwab eConfirms"' in query
    assert "after:" not in query


def test_factory_mapping_adapter_passes_account_suffix_keyword(wired, monkeypatch) -> None:
    """工厂把消息处理器的 suffix 参数转成映射解析器要求的 account_suffix。"""
    connection, dependencies = wired
    connection.execute(
        """
        INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Primary', 'TST001', '276')
        """,
        [ACCOUNT_ID, USER_ID],
    )
    row = dependencies.load_connection(CONNECTION_ID)
    assert row is not None

    def invoke(records, *, dependencies, explicit_account_id):
        accounts = dependencies.load_accounts()
        mapping = dependencies.resolve_mapping(
            suffix="276", accounts=accounts, existing_mapping=None,
            explicit_account_id=explicit_account_id,
        )
        assert mapping.account_id == ACCOUNT_ID
        return SimpleNamespace(to_dict=lambda: {"mapped": 1})

    import lyra.application.gmail_message_processing as processing

    monkeypatch.setattr(processing, "process_gmail_messages", invoke)
    result = dependencies.process_messages(
        row, records=(), explicit_account_id=ACCOUNT_ID
    )

    assert result == {"mapped": 1}


def test_factory_default_message_parse_does_not_read_the_ledger(wired, monkeypatch) -> None:
    """普通邮件继续使用原领域解析，不为其引入账本查询。"""
    import lyra.domain.gmail.econfirm as econfirm
    import lyra.infrastructure.gmail.service_factory as factory
    from lyra.domain.gmail.econfirm import GmailMessage

    _connection, dependencies = wired
    parsed = (object(),)
    monkeypatch.setattr(econfirm, "parse_econfirm", lambda message: parsed)

    def unexpected_ledger_read(*_args, **_kwargs):
        pytest.fail("普通成交不应读取账户账本")

    monkeypatch.setattr(factory, "_account_transactions", unexpected_ledger_read)
    message = GmailMessage(
        message_id="ordinary-message", sender="Schwab", subject="Schwab eConfirms",
        body="ordinary stock trade", email_timestamp=None,
    )

    assert dependencies._parse_message(
        SimpleNamespace(id=CONNECTION_ID), message, None
    ) == parsed


def test_cash_option_fallback_uses_only_mapped_owner_account_sales(wired, monkeypatch) -> None:
    """方向回退仅读当前连接已映射到账户的 option sells，并保留用户隔离。"""
    import lyra.domain.gmail.econfirm as econfirm
    import lyra.infrastructure.gmail.service_factory as factory

    connection, dependencies = wired
    current_key = "TST001"
    connection.execute(
        """
        INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Primary', ?, '276')
        """,
        [ACCOUNT_ID, USER_ID, current_key],
    )
    connection.execute(
        """
        INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Other', 'OTHER999', '999')
        """,
        [OTHER_ACCOUNT_ID, USER_ID],
    )
    connection.execute(
        """
        INSERT INTO users (id, display_name, status, role)
        VALUES (CAST(? AS UUID), 'Other owner', 'active', 'member')
        """,
        [OTHER_USER_ID],
    )
    connection.execute(
        """
        INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Foreign', ?, '276')
        """,
        [OTHER_USERS_ACCOUNT_ID, OTHER_USER_ID, current_key],
    )
    create_gmail_account_mapping_row(
        connection, USER_ID, connection_id=CONNECTION_ID,
        account_suffix="276", account_id=ACCOUNT_ID,
        at=datetime(2026, 10, 16, tzinfo=UTC),
    )
    row = dependencies.load_connection(CONNECTION_ID)
    assert row is not None

    from lyra.domain.gmail.econfirm import GmailMessage, UnresolvedCashOptionSaleError

    confirmed_sale = SimpleNamespace(
        asset_type="option", action="Sell to Open", account=current_key
    )
    ignored_buy = SimpleNamespace(
        asset_type="option", action="Buy to Open", account=current_key
    )
    ignored_stock_sale = SimpleNamespace(
        asset_type="stock", action="Sell to Open", account=current_key
    )
    ledger_reads: list[tuple[str, str]] = []

    def load_account_transactions(db, user_id, legacy_key):
        assert db is connection
        ledger_reads.append((user_id, legacy_key))
        return (confirmed_sale, ignored_buy, ignored_stock_sale)

    parse_calls = 0
    passed_sales: list[tuple] = []

    def parse(message, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        if parse_calls == 1:
            assert kwargs == {}
            raise UnresolvedCashOptionSaleError("cash option sale needs confirmed context")
        assert set(kwargs) == {"existing_option_sales"}
        passed_sales.append(kwargs["existing_option_sales"])
        return ("resolved-sale",)

    monkeypatch.setattr(econfirm, "parse_econfirm", parse)
    monkeypatch.setattr(factory, "_account_transactions", load_account_transactions)
    message = GmailMessage(
        message_id="cash-option-message", sender="Schwab",
        subject="Schwab eConfirms", body="Account ending in 276\n", email_timestamp=None,
    )

    result = dependencies._parse_message(row, message, None)

    assert result == ("resolved-sale",)
    assert ledger_reads == [(USER_ID, current_key)]
    assert passed_sales == [(confirmed_sale,)]
    # 回退只读取账户上下文；原流程的正式映射仍留给导入阶段写入。
    assert len(load_gmail_account_mappings(connection, USER_ID, CONNECTION_ID)) == 1


def test_cash_option_fallback_without_mapping_requires_explicit_account(wired, monkeypatch) -> None:
    """无映射时没有账户事实可供方向判定，且解析回退不自行创建映射。"""
    import lyra.domain.gmail.econfirm as econfirm
    import lyra.infrastructure.gmail.service_factory as factory
    from lyra.domain.gmail.econfirm import GmailMessage, UnresolvedCashOptionSaleError

    connection, dependencies = wired
    connection.execute(
        """
        INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Primary', 'TST001', '276')
        """,
        [ACCOUNT_ID, USER_ID],
    )
    row = dependencies.load_connection(CONNECTION_ID)
    assert row is not None

    def unresolved(_message, **kwargs):
        if kwargs:
            raise AssertionError("无映射时不能读取交易上下文")
        raise UnresolvedCashOptionSaleError("cash option sale needs confirmed context")

    monkeypatch.setattr(econfirm, "parse_econfirm", unresolved)
    monkeypatch.setattr(
        factory, "_account_transactions",
        lambda *_args, **_kwargs: pytest.fail("映射前不能读取任何账户交易"),
    )
    message = GmailMessage(
        message_id="cash-option-message", sender="Schwab",
        subject="Schwab eConfirms", body="Account ending in 276\n", email_timestamp=None,
    )

    with pytest.raises(ApplicationError) as raised:
        dependencies._parse_message(row, message, None)

    assert raised.value.code == "ACCOUNT_MAPPING_REQUIRED"
    assert load_gmail_account_mappings(connection, USER_ID, CONNECTION_ID) == ()


def test_cash_option_explicit_account_context_does_not_create_mapping(wired, monkeypatch) -> None:
    """显式选中已有账户可提供确认事实，但回退解析阶段仍不写映射。"""
    import lyra.domain.gmail.econfirm as econfirm
    import lyra.infrastructure.gmail.service_factory as factory
    from lyra.domain.gmail.econfirm import GmailMessage, UnresolvedCashOptionSaleError

    connection, dependencies = wired
    current_key = "TST001"
    connection.execute(
        """
        INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Primary', ?, '276')
        """,
        [ACCOUNT_ID, USER_ID, current_key],
    )
    row = dependencies.load_connection(CONNECTION_ID)
    assert row is not None
    confirmed_sale = SimpleNamespace(asset_type="option", action="Sell to Close")
    calls = 0

    def parse(_message, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise UnresolvedCashOptionSaleError("cash option sale needs confirmed context")
        assert kwargs["existing_option_sales"] == (confirmed_sale,)
        return ("resolved-sale",)

    monkeypatch.setattr(econfirm, "parse_econfirm", parse)
    monkeypatch.setattr(
        factory, "_account_transactions",
        lambda _db, user_id, legacy_key: (
            (confirmed_sale,) if (user_id, legacy_key) == (USER_ID, current_key) else ()
        ),
    )
    message = GmailMessage(
        message_id="cash-option-message", sender="Schwab",
        subject="Schwab eConfirms", body="Account ending in 276\n", email_timestamp=None,
    )

    assert dependencies._parse_message(row, message, ACCOUNT_ID) == ("resolved-sale",)
    assert load_gmail_account_mappings(connection, USER_ID, CONNECTION_ID) == ()


def test_factory_import_result_reports_actual_inserted_and_replayed_rows(wired) -> None:
    """工厂使用事务提交的 inserted/skipped 行数，并对同 message ID 重放返回零新增。"""
    from lyra.application.gmail_message_processing import GmailMessageImportOutcome
    from lyra.domain.gmail.econfirm import EmailTrade, GmailMessage

    connection, dependencies = wired
    connection.execute(
        """
        INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), CAST(? AS UUID), 'Primary', 'TST001', '276')
        """,
        [ACCOUNT_ID, USER_ID],
    )
    row = dependencies.load_connection(CONNECTION_ID)
    assert row is not None
    import_message = dependencies._import_message(row)
    message = GmailMessage(
        message_id="factory-message", sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms", body="stable body",
        email_timestamp="2026-10-12T09:00:00.000Z",
    )
    trades = (EmailTrade(
        trade_date="2026-10-12", action="Buy", symbol="INTC",
        description="INTEL CORP", quantity="10", price="40.5",
        fees="0", amount="-405",
    ),)
    mapping = SimpleNamespace(account_id=ACCOUNT_ID, legacy_account_key="TST001")

    first = import_message(message, trades, mapping)
    replay = import_message(message, trades, mapping)

    assert first == GmailMessageImportOutcome(
        kind="imported", inserted_transactions=1, existing_transactions=0
    )
    assert replay == GmailMessageImportOutcome(
        kind="already_imported", inserted_transactions=0, existing_transactions=1
    )


@pytest.mark.parametrize(
    ("user_id", "user_status", "device_status", "revoked_at", "code"),
    [
        (USER_ID, "active", "active", None, None),
        (USER_ID, "active", "revoked", STAMP, "DEVICE_CREDENTIAL_INVALID"),
        (USER_ID, "disabled", "active", None, "USER_DISABLED"),
        ("99999999-9999-4999-8999-999999999999", "active", "active", None,
         "DEVICE_CREDENTIAL_INVALID"),
    ],
)
def test_oauth_callback_requires_active_device_and_owner(
    wired, monkeypatch, user_id, user_status, device_status, revoked_at, code
) -> None:
    """OAuth 回调只允许原属主的活动用户和未撤销设备继续。"""
    connection, _sync_dependencies = wired
    dependencies = _Dependencies(
        connection=connection,
        cipher=None,
        now=lambda: datetime(2026, 10, 16, tzinfo=UTC),
        post_form=lambda *_args, **_kwargs: None,
        get_json=lambda *_args, **_kwargs: None,
        get_oauth_json=lambda *_args, **_kwargs: None,
    )

    class DeviceRepositoryStub:
        def __init__(self, _connection):
            pass

        def load_paired_device_auth(self, device_id):
            assert device_id == "device-1"
            return SimpleNamespace(status=device_status, revoked_at=revoked_at)

        def load_device_user(self, device_id):
            assert device_id == "device-1"
            return SimpleNamespace(id=user_id, status=user_status)

    monkeypatch.setattr(
        "lyra.application.gmail_user_scope.DuckDbDeviceRepository",
        DeviceRepositoryStub,
    )

    if code is None:
        dependencies.assert_authorization_active(USER_ID, "device-1")
    else:
        with pytest.raises(ApplicationError) as raised:
            dependencies.assert_authorization_active(USER_ID, "device-1")
        assert raised.value.code == code
