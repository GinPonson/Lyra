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

import pytest

from lyra.application.app_error import ApplicationError
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.gmail.service_factory import _SyncDependencies
from lyra.security.secret_cipher import SecretCipher, master_key_from_hex

USER_ID = "11111111-1111-4111-8111-111111111111"
CONNECTION_ID = "22222222-2222-4222-8222-222222222222"
STAMP = "2026-10-16T09:00:00.000Z"


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
