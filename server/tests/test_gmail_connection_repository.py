"""Gmail 连接与映射仓储测试。

三条 schema 约束在这里被验证：

1. ``UNIQUE (user_id, google_subject)``——**同一用户**的一个 Google 账号只有一条
   连接，而同一个 Google 账号在两个用户下各自独立存储、不互相顶掉；
2. ``UNIQUE (connection_id, account_suffix)``——同一连接内一个尾号一个账户；
3. 映射改绑用**旧账户 ID 做比较并交换**，并发改动报冲突而不是静默覆盖。

多用户下还固定两条隔离性质：

- 每条连接查询/写入都带 ``user_id``，A 读不到、也改不了 B 的连接；
- 映射的建立与改绑要求 connection 与 account **同属一个用户**，跨归属时一条都不写。
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

# 固定的用户 UUID：库本身是每个测试新建的，固定值让断言可以直接引用而不必到处取。
USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture()
def database(tmp_path: Path):
    """打开一个空库，建立两个用户，并给每个用户各建两个账户。

    **每个用户都有自己的账户**：账户标识已在用户内唯一，跨归属映射测试需要一个
    "属于 B 的账户"，它只能由 B 自己拥有。
    """
    handle = open_lyra_database(str(tmp_path / "gmail.duckdb"))
    for user_id, name in ((USER_A, "User A"), (USER_B, "User B")):
        handle.connection.execute(
            "INSERT INTO users (id, display_name) VALUES (CAST(? AS UUID), ?)",
            [user_id, name],
        )
    for user_id in (USER_A, USER_B):
        for suffix in ("276", "999"):
            handle.connection.execute(
                """
                INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
                VALUES (CAST(? AS UUID), CAST(? AS UUID), ?, ?, ?)
                """,
                [str(uuid.uuid4()), user_id, f"Account {suffix}", f"TST{suffix}", suffix],
            )
    try:
        yield handle
    finally:
        handle.close()


def _account_ids(database, user_id: str) -> list[str]:
    """读取某个用户按尾号排序的全部账户 ID。"""
    rows = database.connection.execute(
        "SELECT id FROM accounts WHERE user_id = CAST(? AS UUID) ORDER BY masked_suffix",
        [user_id],
    ).fetchall()
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
    create_gmail_connection_row(database.connection, USER_A, record)

    loaded = load_gmail_connection(database.connection, USER_A, record.id)

    assert loaded is not None
    assert loaded.google_subject == "subject-1"
    assert loaded.google_email == "owner@example.com"
    assert loaded.encrypted_refresh_token == "v1.iv.ct.tag"
    assert loaded.granted_scopes == GMAIL_READONLY_SCOPE
    assert loaded.status == "pending_initial_scan"
    assert loaded.history_id is None
    assert loaded.created_at == "2026-10-12T09:00:00.000Z"
    # 归属落在插入时指定的用户上。
    owner = database.connection.execute(
        "SELECT user_id FROM gmail_connections WHERE id = CAST(? AS UUID)", [record.id]
    ).fetchone()
    assert owner is not None and str(owner[0]) == USER_A


def test_unknown_connection_returns_none(database) -> None:
    """未知 ID 返回 None 而不是抛错。"""
    assert load_gmail_connection(database.connection, USER_A, str(uuid.uuid4())) is None


def test_another_users_connection_is_indistinguishable_from_missing(database) -> None:
    """**A 读不到 B 的连接**：返回 ``None``，与"不存在"不可区分。

    区分二者会让攻击者靠状态码枚举出他人连接是否存在。
    """
    record = _record(google_subject="subject-b")
    create_gmail_connection_row(database.connection, USER_B, record)

    assert load_gmail_connection(database.connection, USER_A, record.id) is None
    assert load_gmail_connection(database.connection, USER_B, record.id) is not None


def test_connections_are_listed_per_user(database) -> None:
    """列表按创建时间排序，且**只看得到自己的连接**。"""
    first = _record(google_subject="s1", created_at="2026-10-12T09:00:00.000Z")
    second = _record(
        google_subject="s2", created_at="2026-10-12T10:00:00.000Z",
        updated_at="2026-10-12T10:00:00.000Z",
    )
    other_users = _record(
        google_subject="s3", google_email="b@example.com",
        created_at="2026-10-12T08:00:00.000Z",
    )
    create_gmail_connection_row(database.connection, USER_A, first)
    create_gmail_connection_row(database.connection, USER_A, second)
    create_gmail_connection_row(database.connection, USER_B, other_users)

    assert [
        row.google_subject
        for row in load_gmail_connections(database.connection, USER_A)
    ] == ["s1", "s2"]
    assert [
        row.google_subject
        for row in load_gmail_connections(database.connection, USER_B)
    ] == ["s3"]


def test_a_google_account_can_only_have_one_connection_per_user(database) -> None:
    """**同一用户下同一 Google 账号只能有一条连接。**

    否则同一账号会出现两条连接，同步游标互相覆盖。
    """
    create_gmail_connection_row(database.connection, USER_A, _record())

    with pytest.raises(ApplicationError) as raised:
        create_gmail_connection_row(
            database.connection, USER_A, _record(id=str(uuid.uuid4()))
        )

    assert raised.value.status_code == 409
    assert raised.value.retryable is False
    # 报错文案按用户表述：全局"该 Google 账号已有连接"在多用户下是错的。
    assert raised.value.message == "该 Google 账号在当前用户下已有连接"


def test_the_same_google_account_is_stored_independently_per_user(database) -> None:
    """**同一个 Google 账号在两个用户下各自独立存储，不互相顶掉。**

    唯一键是 ``(user_id, google_subject)``。若沿用全局 ``google_subject`` UNIQUE，
    B 授权同一个 Google 账号时 A 的连接会消失（A 的令牌与同步游标被 B 覆盖）——
    而两个用户各自连接自己的工作邮箱是正常用法（P0 契约 §3.12）。
    """
    shared_subject = "same-google-subject"
    connection_a = _record(google_subject=shared_subject, google_email="a@example.com")
    connection_b = _record(
        id=str(uuid.uuid4()), google_subject=shared_subject, google_email="b@example.com"
    )

    create_gmail_connection_row(database.connection, USER_A, connection_a)
    # 不报冲突：唯一键按用户划分。
    create_gmail_connection_row(database.connection, USER_B, connection_b)

    loaded_a = load_gmail_connection(database.connection, USER_A, connection_a.id)
    loaded_b = load_gmail_connection(database.connection, USER_B, connection_b.id)
    assert loaded_a is not None and loaded_a.google_email == "a@example.com"
    assert loaded_b is not None and loaded_b.google_email == "b@example.com"
    # 两条行各自存在，A 的连接没有被 B 的授权改写成 B 的邮箱。
    assert [row.id for row in load_gmail_connections(database.connection, USER_A)] == [
        connection_a.id
    ]
    assert [row.id for row in load_gmail_connections(database.connection, USER_B)] == [
        connection_b.id
    ]


def test_saving_a_disconnected_connection_clears_the_token(database) -> None:
    """断开后保存：**令牌被清空且状态为 disconnected**（满足 schema 的 CHECK）。"""
    record = _record()
    create_gmail_connection_row(database.connection, USER_A, record)

    from lyra.infrastructure.gmail.connection_credential import (
        disconnect_gmail_connection,
    )

    disconnected = disconnect_gmail_connection(record, LATER)
    save_gmail_connection_row(
        database.connection, USER_A, disconnected, expected_updated_at=record.updated_at
    )

    loaded = load_gmail_connection(database.connection, USER_A, record.id)
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
    create_gmail_connection_row(database.connection, USER_A, record)

    from dataclasses import replace

    with pytest.raises(ApplicationError) as raised:
        save_gmail_connection_row(
            database.connection, USER_A, replace(record, status="active"),
            expected_updated_at="2020-01-01T00:00:00.000Z",
        )

    assert raised.value.status_code == 409
    assert raised.value.retryable is True


def test_another_user_cannot_save_the_connection(database) -> None:
    """**A 不能写 B 的连接**，即使 A 读到了正确的 ``updated_at``。

    只有 CAS 没有用户范围时，A 拿到 B 的连接 ID 与其 ``updated_at`` 就能改写 B 的
    连接状态（例如把"已断开"改回"活动中"）。
    """
    from dataclasses import replace

    record = _record(google_subject="subject-b")
    create_gmail_connection_row(database.connection, USER_B, record)

    with pytest.raises(ApplicationError) as raised:
        save_gmail_connection_row(
            database.connection, USER_A,
            replace(record, status="active", updated_at="2026-10-13T09:00:00.000Z"),
            expected_updated_at=record.updated_at,
        )

    assert raised.value.status_code == 409
    loaded = load_gmail_connection(database.connection, USER_B, record.id)
    assert loaded is not None
    # B 的行完全没被改动。
    assert loaded.status == "pending_initial_scan"
    assert loaded.updated_at == "2026-10-12T09:00:00.000Z"


def test_saving_never_rewrites_the_google_identity(database) -> None:
    """**身份列不可变。**

    允许同步过程改写绑定，就等于允许一次同步把连接悄悄指向另一个邮箱——
    而之后的每封邮件都来自那个新邮箱。
    """
    from dataclasses import replace

    record = _record()
    create_gmail_connection_row(database.connection, USER_A, record)

    save_gmail_connection_row(
        database.connection, USER_A,
        replace(record, google_subject="hijacked", google_email="other@example.com",
                status="active", updated_at=LATER.isoformat().replace("+00:00", "Z")),
        expected_updated_at=record.updated_at,
    )

    loaded = load_gmail_connection(database.connection, USER_A, record.id)
    assert loaded is not None
    assert loaded.google_subject == "subject-1"
    assert loaded.google_email == "owner@example.com"


def test_saving_never_rewrites_the_owner(database) -> None:
    """**归属列不可变**：保存路径不接受 user_id，也就不可能把连接转手给另一个用户。"""
    from dataclasses import replace

    record = _record()
    create_gmail_connection_row(database.connection, USER_A, record)

    save_gmail_connection_row(
        database.connection, USER_A, replace(record, status="active"),
        expected_updated_at=record.updated_at,
    )

    owner = database.connection.execute(
        "SELECT user_id FROM gmail_connections WHERE id = CAST(? AS UUID)", [record.id]
    ).fetchone()
    assert owner is not None and str(owner[0]) == USER_A


def test_connection_without_a_token_must_be_disconnected(database) -> None:
    """**未断开的连接必须有令牌**——它既不能用也无法重连。

    这里在插入前显式校验，因此报的是**准确的领域错误**，而不是笼统的数据库约束
    异常。若只依赖 schema 的 CHECK，这条违规会和唯一约束一样抛
    ``ConstraintException``，被唯一约束的分支误报成"账号已有连接"——
    **把运维引向完全错误的方向**。
    """
    with pytest.raises(ValueError, match="必须有 refresh token"):
        create_gmail_connection_row(
            database.connection, USER_A,
            _record(status="connected", encrypted_refresh_token=None),
        )


def test_schema_still_enforces_the_token_invariant(database) -> None:
    """schema 的 CHECK 仍然是最后一道防线。"""
    import duckdb

    with pytest.raises(duckdb.ConstraintException):
        database.connection.execute(
            """
            INSERT INTO gmail_connections (
                id, user_id, google_subject, google_email, encrypted_refresh_token,
                granted_scopes, status
            ) VALUES (
                CAST(? AS UUID), CAST(? AS UUID), 'raw-subject', 'a@b.com', NULL, 's', 'connected'
            )
            """,
            [str(uuid.uuid4()), USER_A],
        )


def test_other_constraint_violations_are_not_reported_as_duplicate_connections(
    database,
) -> None:
    """**非唯一键的约束违规不被误报成"账号已有连接"。**

    外键（user_id 指向不存在的用户）与 CHECK 同样抛 ``ConstraintException``；
    若不加区分地一律报 409"已有连接"，运维会去检查一个根本不存在的问题。
    """
    import duckdb

    with pytest.raises(duckdb.ConstraintException):
        create_gmail_connection_row(
            database.connection, str(uuid.uuid4()), _record()
        )


# --------------------------------------------------------------------------
# 账户映射
# --------------------------------------------------------------------------


def test_creates_and_reads_a_mapping(database) -> None:
    """映射可建立并读回。"""
    connection = _record()
    create_gmail_connection_row(database.connection, USER_A, connection)
    account_id = _account_ids(database, USER_A)[0]

    mapping = create_gmail_account_mapping_row(
        database.connection, USER_A, connection_id=connection.id,
        account_suffix="276", account_id=account_id, at=NOW,
    )

    assert mapping.account_suffix == "276"
    assert mapping.account_id == account_id
    assert mapping.created_at == "2026-10-12T09:00:00.000Z"
    loaded = load_gmail_account_mapping(database.connection, USER_A, mapping.id)
    assert loaded == mapping


def test_a_suffix_can_only_map_to_one_account_per_connection(database) -> None:
    """**同一连接内一个尾号只能映射到一个账户。**"""
    connection = _record()
    create_gmail_connection_row(database.connection, USER_A, connection)
    first, second = _account_ids(database, USER_A)
    create_gmail_account_mapping_row(
        database.connection, USER_A, connection_id=connection.id,
        account_suffix="276", account_id=first, at=NOW,
    )

    with pytest.raises(ApplicationError) as raised:
        create_gmail_account_mapping_row(
            database.connection, USER_A, connection_id=connection.id,
            account_suffix="276", account_id=second, at=NOW,
        )

    assert raised.value.status_code == 409
    assert raised.value.retryable is False


def test_cross_user_mapping_writes_nothing(database) -> None:
    """**A 的连接不能映射到 B 的账户**，且一条行都不写。

    这条跨归属映射若落库，A 之后收到的邮件会把交易写进 B 的账本，而更正它意味着
    删除已入账的交易并重算 FIFO——因此必须整体失败（P5 完成条件）。
    """
    connection = _record(google_subject="subject-a")
    create_gmail_connection_row(database.connection, USER_A, connection)
    other_users_account = _account_ids(database, USER_B)[0]

    with pytest.raises(ApplicationError) as raised:
        create_gmail_account_mapping_row(
            database.connection, USER_A, connection_id=connection.id,
            account_suffix="276", account_id=other_users_account, at=NOW,
        )

    assert raised.value.status_code == 404
    remaining = database.connection.execute(
        "SELECT count(*) FROM gmail_account_mappings"
    ).fetchone()
    assert remaining is not None and remaining[0] == 0, "跨归属映射不得留下部分写入"


def test_mapping_into_another_users_connection_writes_nothing(database) -> None:
    """**不能在 B 的连接下建映射**，即使目标账户属于 B。

    少这一层时，A 只要拿到 B 的连接 ID 就能往 B 的连接里塞一条映射，
    从而影响 B 的邮件入账路径。
    """
    connection = _record(google_subject="subject-b")
    create_gmail_connection_row(database.connection, USER_B, connection)
    other_account = _account_ids(database, USER_B)[0]

    with pytest.raises(ApplicationError) as raised:
        create_gmail_account_mapping_row(
            database.connection, USER_A, connection_id=connection.id,
            account_suffix="276", account_id=other_account, at=NOW,
        )

    assert raised.value.status_code == 404
    remaining = database.connection.execute(
        "SELECT count(*) FROM gmail_account_mappings"
    ).fetchone()
    assert remaining is not None and remaining[0] == 0


def test_different_connections_may_map_the_same_suffix(database) -> None:
    """不同连接可以映射同一个尾号——那可能是两个不同的券商账户。"""
    first_connection = _record(google_subject="s1")
    second_connection = _record(google_subject="s2")
    create_gmail_connection_row(database.connection, USER_A, first_connection)
    create_gmail_connection_row(database.connection, USER_A, second_connection)
    account_id = _account_ids(database, USER_A)[0]

    create_gmail_account_mapping_row(
        database.connection, USER_A, connection_id=first_connection.id,
        account_suffix="276", account_id=account_id, at=NOW,
    )
    create_gmail_account_mapping_row(
        database.connection, USER_A, connection_id=second_connection.id,
        account_suffix="276", account_id=account_id, at=NOW,
    )

    assert len(
        load_gmail_account_mappings(database.connection, USER_A, first_connection.id)
    ) == 1
    assert len(
        load_gmail_account_mappings(database.connection, USER_A, second_connection.id)
    ) == 1


def test_mappings_of_another_users_connection_are_not_visible(database) -> None:
    """**A 列不出 B 连接的映射**；返回空列表，与"没有映射"不可区分。"""
    connection = _record(google_subject="subject-b")
    create_gmail_connection_row(database.connection, USER_B, connection)
    account_id = _account_ids(database, USER_B)[0]
    mapping = create_gmail_account_mapping_row(
        database.connection, USER_B, connection_id=connection.id,
        account_suffix="276", account_id=account_id, at=NOW,
    )

    assert load_gmail_account_mappings(
        database.connection, USER_A, connection.id
    ) == ()
    assert load_gmail_account_mapping(database.connection, USER_A, mapping.id) is None


def test_blank_suffix_is_rejected(database) -> None:
    """空白尾号必须拒绝。"""
    connection = _record()
    create_gmail_connection_row(database.connection, USER_A, connection)

    with pytest.raises(ValueError, match="尾号不能为空"):
        create_gmail_account_mapping_row(
            database.connection, USER_A, connection_id=connection.id,
            account_suffix="   ", account_id=_account_ids(database, USER_A)[0], at=NOW,
        )


def test_mappings_are_listed_by_suffix(database) -> None:
    """列表按尾号排序，便于展示。"""
    connection = _record()
    create_gmail_connection_row(database.connection, USER_A, connection)
    account_id = _account_ids(database, USER_A)[0]
    for suffix in ("999", "276"):
        create_gmail_account_mapping_row(
            database.connection, USER_A, connection_id=connection.id,
            account_suffix=suffix, account_id=account_id, at=NOW,
        )

    assert [
        row.account_suffix
        for row in load_gmail_account_mappings(database.connection, USER_A, connection.id)
    ] == ["276", "999"]


# --------------------------------------------------------------------------
# 改绑 CAS
# --------------------------------------------------------------------------


def test_rebinding_with_the_current_account_succeeds(database) -> None:
    """以正确的旧账户改绑成功。"""
    connection = _record()
    create_gmail_connection_row(database.connection, USER_A, connection)
    first, second = _account_ids(database, USER_A)
    mapping = create_gmail_account_mapping_row(
        database.connection, USER_A, connection_id=connection.id,
        account_suffix="276", account_id=first, at=NOW,
    )

    update_gmail_account_mapping_row(
        database.connection, USER_A, mapping_id=mapping.id,
        expected_account_id=first, next_account_id=second, at=LATER,
    )

    loaded = load_gmail_account_mapping(database.connection, USER_A, mapping.id)
    assert loaded is not None
    assert loaded.account_id == second
    assert loaded.updated_at == "2026-10-13T09:00:00.000Z"


def test_rebinding_with_a_stale_account_is_a_conflict(database) -> None:
    """**旧账户 ID 不匹配时冲突**——先到的一方已改过。

    静默覆盖会让先到设备的改动消失，而两台设备都以为保存成功了。
    """
    connection = _record()
    create_gmail_connection_row(database.connection, USER_A, connection)
    first, second = _account_ids(database, USER_A)
    mapping = create_gmail_account_mapping_row(
        database.connection, USER_A, connection_id=connection.id,
        account_suffix="276", account_id=first, at=NOW,
    )

    with pytest.raises(ApplicationError) as raised:
        update_gmail_account_mapping_row(
            database.connection, USER_A, mapping_id=mapping.id,
            expected_account_id=second, next_account_id=first, at=LATER,
        )

    assert raised.value.status_code == 409
    assert raised.value.retryable is True


def test_rebinding_onto_another_users_account_is_a_conflict(database) -> None:
    """**不能把映射改绑到别人的账户**，且原映射保持不变。"""
    connection = _record()
    create_gmail_connection_row(database.connection, USER_A, connection)
    own_account = _account_ids(database, USER_A)[0]
    mapping = create_gmail_account_mapping_row(
        database.connection, USER_A, connection_id=connection.id,
        account_suffix="276", account_id=own_account, at=NOW,
    )
    other_users_account = _account_ids(database, USER_B)[0]

    with pytest.raises(ApplicationError) as raised:
        update_gmail_account_mapping_row(
            database.connection, USER_A, mapping_id=mapping.id,
            expected_account_id=own_account, next_account_id=other_users_account,
            at=LATER,
        )

    assert raised.value.status_code == 409
    loaded = load_gmail_account_mapping(database.connection, USER_A, mapping.id)
    assert loaded is not None and loaded.account_id == own_account


def test_another_user_cannot_rebind_the_mapping(database) -> None:
    """**A 不能改 B 的映射**，即使 A 知道映射 ID 与当前账户。"""
    connection = _record(google_subject="subject-b")
    create_gmail_connection_row(database.connection, USER_B, connection)
    first, second = _account_ids(database, USER_B)
    mapping = create_gmail_account_mapping_row(
        database.connection, USER_B, connection_id=connection.id,
        account_suffix="276", account_id=first, at=NOW,
    )

    with pytest.raises(ApplicationError) as raised:
        update_gmail_account_mapping_row(
            database.connection, USER_A, mapping_id=mapping.id,
            expected_account_id=first, next_account_id=second, at=LATER,
        )

    assert raised.value.status_code == 409
    loaded = load_gmail_account_mapping(database.connection, USER_B, mapping.id)
    assert loaded is not None and loaded.account_id == first


def test_unknown_mapping_rebinding_is_a_conflict(database) -> None:
    """未知映射同样报冲突，不泄漏"是否存在"。"""
    with pytest.raises(ApplicationError) as raised:
        update_gmail_account_mapping_row(
            database.connection, USER_A, mapping_id=str(uuid.uuid4()),
            expected_account_id=str(uuid.uuid4()), next_account_id=str(uuid.uuid4()),
            at=NOW,
        )

    assert raised.value.code == "GMAIL_CONNECTION_ERROR"
