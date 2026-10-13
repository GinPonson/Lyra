"""Gmail 连接与映射仓储测试。

三条 schema 约束在这里被验证：

1. ``google_subject`` UNIQUE——一个 Google 账号只有一条连接；
2. ``UNIQUE (connection_id, account_suffix)``——同一连接内一个尾号一个账户；
3. 映射改绑用**旧账户 ID 做比较并交换**，并发改动报冲突而不是静默覆盖。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lyra.application.app_error import ApplicationError
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.db.gmail_connection_repository import (
    create_gmail_account_mapping_row,
    create_gmail_connection_row,
    load_gmail_account_mapping,
    load_gmail_account_mappings,
    load_gmail_connection,
    load_gmail_connections,
    save_gmail_connection_row,
    update_gmail_account_mapping_row,
)
from lyra.infrastructure.gmail.connection_credential import (
    GMAIL_READONLY_SCOPE,
    GmailConnectionRecord,
)

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 13, 9, 0, 0, tzinfo=UTC)


@pytest.fixture()
def database(tmp_path: Path):
    """打开一个空库并建立两个账户。"""
    handle = open_lyra_database(str(tmp_path / "gmail.duckdb"))
    for suffix in ("276", "999"):
        handle.connection.execute(
            """
            INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
            VALUES (CAST(? AS UUID), ?, ?, ?)
            """,
            [str(uuid.uuid4()), f"Account {suffix}", f"TST{suffix}", suffix],
        )
    try:
        yield handle
    finally:
        handle.close()


def _account_ids(database) -> list[str]:
    """读取全部账户 ID。"""
    rows = database.connection.execute("SELECT id FROM accounts ORDER BY masked_suffix").fetchall()
    return [str(row[0]) for row in rows]


def _record(**overrides) -> GmailConnectionRecord:
    """构造一条待插入的连接记录。"""
    base = {
        "id": str(uuid.uuid4()), "google_subject": "subject-1",
        "google_email": "owner@example.com",
        "encrypted_refresh_token": "v1.iv.ct.tag",
        "granted_scopes": GMAIL_READONLY_SCOPE, "history_id": None,
        "status": "pending_initial_scan", "last_error_code": None,
        "last_sync_at": None, "created_at": "2026-10-12T09:00:00.000Z",
        "updated_at": "2026-10-12T09:00:00.000Z",
    }
    base.update(overrides)
    return GmailConnectionRecord(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------


def test_inserts_and_reads_back_a_connection(database) -> None:
    """插入后可逐字段读回。"""
    record = _record()
    create_gmail_connection_row(database.connection, record)

    loaded = load_gmail_connection(database.connection, record.id)

    assert loaded is not None
    assert loaded.google_subject == "subject-1"
    assert loaded.google_email == "owner@example.com"
    assert loaded.encrypted_refresh_token == "v1.iv.ct.tag"
    assert loaded.granted_scopes == GMAIL_READONLY_SCOPE
    assert loaded.status == "pending_initial_scan"
    assert loaded.history_id is None
    assert loaded.created_at == "2026-10-12T09:00:00.000Z"


def test_unknown_connection_returns_none(database) -> None:
    """未知 ID 返回 None 而不是抛错。"""
    assert load_gmail_connection(database.connection, str(uuid.uuid4())) is None


def test_a_google_account_can_only_have_one_connection(database) -> None:
    """**同一 Google 账号只能有一条连接。**

    否则同一账号会出现两条连接，同步游标互相覆盖。
    """
    create_gmail_connection_row(database.connection, _record())

    with pytest.raises(ApplicationError) as raised:
        create_gmail_connection_row(
            database.connection, _record(id=str(uuid.uuid4()))
        )

    assert raised.value.status_code == 409
    assert raised.value.retryable is False


def test_connections_are_listed_in_creation_order(database) -> None:
    """列表按创建时间排序。"""
    first = _record(google_subject="s1", created_at="2026-10-12T09:00:00.000Z")
    second = _record(
        google_subject="s2", created_at="2026-10-12T10:00:00.000Z",
        updated_at="2026-10-12T10:00:00.000Z",
    )
    create_gmail_connection_row(database.connection, first)
    create_gmail_connection_row(database.connection, second)

    assert [row.google_subject for row in load_gmail_connections(database.connection)] == [
        "s1", "s2"
    ]


def test_saving_a_disconnected_connection_clears_the_token(database) -> None:
    """断开后保存：**令牌被清空且状态为 disconnected**（满足 schema 的 CHECK）。"""
    record = _record()
    create_gmail_connection_row(database.connection, record)

    from lyra.infrastructure.gmail.connection_credential import (
        disconnect_gmail_connection,
    )

    disconnected = disconnect_gmail_connection(record, LATER)
    save_gmail_connection_row(
        database.connection, disconnected, expected_updated_at=record.updated_at
    )

    loaded = load_gmail_connection(database.connection, record.id)
    assert loaded is not None
    assert loaded.encrypted_refresh_token is None
    assert loaded.status == "disconnected"
    assert loaded.updated_at == "2026-10-13T09:00:00.000Z"
    # 历史与作用域保留用于审计。
    assert loaded.granted_scopes == record.granted_scopes


def test_saving_with_a_stale_timestamp_is_a_conflict(database) -> None:
    """**``updated_at`` 不匹配时报冲突。**

    同步是长流程（刷新令牌、拉取消息、写账本），期间用户可能在另一台设备上断开连接。
    不加 CAS 的话，这次同步的收尾会把"已断开"覆盖回"活动中"——**用户会看到连接自己
    又活了**。
    """
    record = _record()
    create_gmail_connection_row(database.connection, record)

    from dataclasses import replace

    with pytest.raises(ApplicationError) as raised:
        save_gmail_connection_row(
            database.connection, replace(record, status="active"),
            expected_updated_at="2020-01-01T00:00:00.000Z",
        )

    assert raised.value.status_code == 409
    assert raised.value.retryable is True


def test_saving_never_rewrites_the_google_identity(database) -> None:
    """**身份列不可变。**

    允许同步过程改写绑定，就等于允许一次同步把连接悄悄指向另一个邮箱——
    而之后的每封邮件都来自那个新邮箱。
    """
    from dataclasses import replace

    record = _record()
    create_gmail_connection_row(database.connection, record)

    save_gmail_connection_row(
        database.connection,
        replace(record, google_subject="hijacked", google_email="other@example.com",
                status="active", updated_at=LATER.isoformat().replace("+00:00", "Z")),
        expected_updated_at=record.updated_at,
    )

    loaded = load_gmail_connection(database.connection, record.id)
    assert loaded is not None
    assert loaded.google_subject == "subject-1"
    assert loaded.google_email == "owner@example.com"


def test_connection_without_a_token_must_be_disconnected(database) -> None:
    """**未断开的连接必须有令牌**——它既不能用也无法重连。

    这里在插入前显式校验，因此报的是**准确的领域错误**，而不是笼统的数据库约束
    异常。若只依赖 schema 的 CHECK，这条违规会和唯一约束一样抛
    ``ConstraintException``，被唯一约束的分支误报成"账号已有连接"——
    **把运维引向完全错误的方向**。
    """
    with pytest.raises(ValueError, match="必须有 refresh token"):
        create_gmail_connection_row(
            database.connection,
            _record(status="connected", encrypted_refresh_token=None),
        )


def test_schema_still_enforces_the_token_invariant(database) -> None:
    """schema 的 CHECK 仍然是最后一道防线。"""
    import duckdb

    with pytest.raises(duckdb.ConstraintException):
        database.connection.execute(
            """
            INSERT INTO gmail_connections (
                id, google_subject, google_email, encrypted_refresh_token,
                granted_scopes, status
            ) VALUES (
                CAST(? AS UUID), 'raw-subject', 'a@b.com', NULL, 's', 'connected'
            )
            """,
            [str(uuid.uuid4())],
        )


# --------------------------------------------------------------------------
# 账户映射
# --------------------------------------------------------------------------


def test_creates_and_reads_a_mapping(database) -> None:
    """映射可建立并读回。"""
    connection = _record()
    create_gmail_connection_row(database.connection, connection)
    account_id = _account_ids(database)[0]

    mapping = create_gmail_account_mapping_row(
        database.connection, connection_id=connection.id,
        account_suffix="276", account_id=account_id, at=NOW,
    )

    assert mapping.account_suffix == "276"
    assert mapping.account_id == account_id
    assert mapping.created_at == "2026-10-12T09:00:00.000Z"
    loaded = load_gmail_account_mapping(database.connection, mapping.id)
    assert loaded == mapping


def test_a_suffix_can_only_map_to_one_account_per_connection(database) -> None:
    """**同一连接内一个尾号只能映射到一个账户。**"""
    connection = _record()
    create_gmail_connection_row(database.connection, connection)
    first, second = _account_ids(database)
    create_gmail_account_mapping_row(
        database.connection, connection_id=connection.id,
        account_suffix="276", account_id=first, at=NOW,
    )

    with pytest.raises(ApplicationError) as raised:
        create_gmail_account_mapping_row(
            database.connection, connection_id=connection.id,
            account_suffix="276", account_id=second, at=NOW,
        )

    assert raised.value.status_code == 409
    assert raised.value.retryable is False


def test_different_connections_may_map_the_same_suffix(database) -> None:
    """不同连接可以映射同一个尾号——那可能是两个不同的券商账户。"""
    first_connection = _record(google_subject="s1")
    second_connection = _record(google_subject="s2")
    create_gmail_connection_row(database.connection, first_connection)
    create_gmail_connection_row(database.connection, second_connection)
    account_id = _account_ids(database)[0]

    create_gmail_account_mapping_row(
        database.connection, connection_id=first_connection.id,
        account_suffix="276", account_id=account_id, at=NOW,
    )
    create_gmail_account_mapping_row(
        database.connection, connection_id=second_connection.id,
        account_suffix="276", account_id=account_id, at=NOW,
    )

    assert len(load_gmail_account_mappings(database.connection, first_connection.id)) == 1
    assert len(load_gmail_account_mappings(database.connection, second_connection.id)) == 1


def test_blank_suffix_is_rejected(database) -> None:
    """空白尾号必须拒绝。"""
    connection = _record()
    create_gmail_connection_row(database.connection, connection)

    with pytest.raises(ValueError, match="尾号不能为空"):
        create_gmail_account_mapping_row(
            database.connection, connection_id=connection.id,
            account_suffix="   ", account_id=_account_ids(database)[0], at=NOW,
        )


def test_mappings_are_listed_by_suffix(database) -> None:
    """列表按尾号排序，便于展示。"""
    connection = _record()
    create_gmail_connection_row(database.connection, connection)
    account_id = _account_ids(database)[0]
    for suffix in ("999", "276"):
        create_gmail_account_mapping_row(
            database.connection, connection_id=connection.id,
            account_suffix=suffix, account_id=account_id, at=NOW,
        )

    assert [
        row.account_suffix
        for row in load_gmail_account_mappings(database.connection, connection.id)
    ] == ["276", "999"]


# --------------------------------------------------------------------------
# 改绑 CAS
# --------------------------------------------------------------------------


def test_rebinding_with_the_current_account_succeeds(database) -> None:
    """以正确的旧账户改绑成功。"""
    connection = _record()
    create_gmail_connection_row(database.connection, connection)
    first, second = _account_ids(database)
    mapping = create_gmail_account_mapping_row(
        database.connection, connection_id=connection.id,
        account_suffix="276", account_id=first, at=NOW,
    )

    update_gmail_account_mapping_row(
        database.connection, mapping_id=mapping.id,
        expected_account_id=first, next_account_id=second, at=LATER,
    )

    loaded = load_gmail_account_mapping(database.connection, mapping.id)
    assert loaded is not None
    assert loaded.account_id == second
    assert loaded.updated_at == "2026-10-13T09:00:00.000Z"


def test_rebinding_with_a_stale_account_is_a_conflict(database) -> None:
    """**旧账户 ID 不匹配时冲突**——先到的一方已改过。

    静默覆盖会让先到设备的改动消失，而两台设备都以为保存成功了。
    """
    connection = _record()
    create_gmail_connection_row(database.connection, connection)
    first, second = _account_ids(database)
    mapping = create_gmail_account_mapping_row(
        database.connection, connection_id=connection.id,
        account_suffix="276", account_id=first, at=NOW,
    )

    with pytest.raises(ApplicationError) as raised:
        update_gmail_account_mapping_row(
            database.connection, mapping_id=mapping.id,
            expected_account_id=second, next_account_id=first, at=LATER,
        )

    assert raised.value.status_code == 409
    assert raised.value.retryable is True


def test_unknown_mapping_rebinding_is_a_conflict(database) -> None:
    """未知映射同样报冲突，不泄漏"是否存在"。"""
    with pytest.raises(ApplicationError) as raised:
        update_gmail_account_mapping_row(
            database.connection, mapping_id=str(uuid.uuid4()),
            expected_account_id=str(uuid.uuid4()), next_account_id=str(uuid.uuid4()),
            at=NOW,
        )

    assert raised.value.code == "GMAIL_CONNECTION_ERROR"
