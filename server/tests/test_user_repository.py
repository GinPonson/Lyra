"""用户与登录身份仓储的 DuckDB 集成测试。

跑在**真实 schema 16** 上，因此这里验证的是 SQL、约束与事务边界的真实行为，而不是
内存假仓储上的假设。重点覆盖三件容易出错的事：

1. ``(provider, subject)`` 的收敛：重复登录不换 owner、并发被抢先时不残留候选用户；
2. 事务语义：2a–2d 要么全成、要么全不成，且嵌套调用不重复 ``BEGIN``；
3. 时间戳必须往返为 **ISO UTC 毫秒 + ``Z`` 文本**——领域层按文本比较时间，
   返回 ``datetime`` 会静默破坏过期判断。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from lyra.application.app_error import ApplicationError
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.user_repository import (
    MAX_USER_PAGE_SIZE,
    UserRepository,
)

NOW = datetime(2026, 10, 16, 7, 30, 0, 123000, tzinfo=UTC)
GOOGLE = LoginIdentity(
    provider="google", subject="112233", email="owner@example.com", display_name="Owner"
)
GITHUB = LoginIdentity(
    provider="github", subject="445566", email="octo@example.com", display_name="Octocat"
)
LINUXDO = LoginIdentity(provider="linuxdo", subject="778899", email=None, display_name=None)

ISO_UTC_MILLISECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture()
def wired(tmp_path: Path):
    """在临时库上装配真实仓储。"""
    database = open_lyra_database(str(tmp_path / "users.duckdb"))
    repository = UserRepository(database.connection)
    try:
        yield repository, database.connection
    finally:
        database.close()


def _count(connection: duckdb.DuckDBPyConnection, table: str) -> int:
    """统计表行数。"""
    row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_find_or_create_registers_member_user(wired) -> None:
    """首次登录必须原子创建 ``member`` 用户与身份，并标记 created。"""
    repository, connection = wired

    resolved = repository.find_or_create_user_for_identity(GOOGLE, now=NOW)

    assert resolved.created is True
    assert resolved.user.role == "member"
    assert resolved.user.status == "active"
    assert resolved.user.display_name == "Owner"
    assert resolved.identity.provider == "google"
    assert resolved.identity.subject == "112233"
    assert resolved.identity.user_id == resolved.user.id
    assert _count(connection, "users") == 1
    assert _count(connection, "user_identities") == 1


def test_repeat_login_returns_same_user_without_created(wired) -> None:
    """同一 (provider, subject) 再次登录必须复用同一 userId，且不改写身份展示信息。"""
    repository, _ = wired
    first = repository.find_or_create_user_for_identity(GOOGLE, now=NOW)

    second = repository.find_or_create_user_for_identity(
        LoginIdentity(
            provider="google",
            subject="112233",
            email="changed@example.com",
            display_name="Renamed",
        ),
        now=NOW + timedelta(days=1),
    )

    assert second.created is False
    assert second.user.id == first.user.id
    assert second.user.display_name == "Owner"
    assert second.identity.id == first.identity.id
    assert second.identity.email == "owner@example.com"
    assert second.identity.updated_at == first.identity.updated_at


def test_different_subject_is_a_separate_user(wired) -> None:
    """同一来源的不同 subject 是不同的人：不得按邮箱或昵称合并。"""
    repository, connection = wired
    first = repository.find_or_create_user_for_identity(GOOGLE, now=NOW)
    second = repository.find_or_create_user_for_identity(
        LoginIdentity(
            provider="google",
            subject="999999",
            email="owner@example.com",
            display_name="Owner",
        ),
        now=NOW,
    )

    assert second.created is True
    assert second.user.id != first.user.id
    assert _count(connection, "users") == 2


def test_identity_subject_is_shared_across_providers_only_by_subject(wired) -> None:
    """不同来源的同名 subject 是两个身份，各得一个用户。"""
    repository, connection = wired
    repository.find_or_create_user_for_identity(GOOGLE, now=NOW)
    repository.find_or_create_user_for_identity(
        LoginIdentity(provider="github", subject="112233", email=None, display_name=None),
        now=NOW,
    )

    assert _count(connection, "users") == 2
    assert _count(connection, "user_identities") == 2


def test_display_name_falls_back_to_email_then_subject(wired) -> None:
    """OAuth 不保证提供昵称，但 users.display_name 不能为空。"""
    repository, _ = wired
    with_email = repository.find_or_create_user_for_identity(GITHUB, now=NOW)
    without_email = repository.find_or_create_user_for_identity(LINUXDO, now=NOW)

    assert with_email.user.display_name == "Octocat"
    assert without_email.user.display_name == "778899"


def test_timestamps_are_iso_utc_milliseconds_text(wired) -> None:
    """仓储必须往返 ``Z`` 结尾的毫秒文本，而不是 datetime 对象。"""
    repository, _ = wired
    resolved = repository.find_or_create_user_for_identity(GOOGLE, now=NOW)

    for value in (
        resolved.user.created_at,
        resolved.user.updated_at,
        resolved.identity.created_at,
        resolved.identity.updated_at,
    ):
        assert isinstance(value, str)
        assert ISO_UTC_MILLISECONDS.match(value), value
    assert resolved.user.created_at == "2026-10-16T07:30:00.123Z"


def test_find_user_by_id_and_missing(wired) -> None:
    """按 id 读取用户；不存在返回 None，格式非法抛 400。"""
    repository, _ = wired
    created = repository.find_or_create_user_for_identity(GOOGLE, now=NOW)

    assert repository.find_user_by_id(created.user.id) == created.user
    assert repository.find_user_by_id("99999999-9999-9999-9999-999999999999") is None
    with pytest.raises(ApplicationError) as error:
        repository.find_user_by_id("not-a-uuid")
    assert error.value.code == "INVALID_ARGUMENT"
    assert error.value.status_code == 400


def test_find_identity(wired) -> None:
    """按 (provider, subject) 读取身份；未登记时返回 None。"""
    repository, _ = wired
    created = repository.find_or_create_user_for_identity(GOOGLE, now=NOW)

    assert repository.find_identity("google", "112233") == created.identity
    assert repository.find_identity("google", "nope") is None
    assert repository.find_identity("github", "112233") is None


def test_set_user_status_disables_and_restores(wired) -> None:
    """停用与恢复都必须落库，且 updated_at 只前进不回退。"""
    repository, _ = wired
    created = repository.find_or_create_user_for_identity(GOOGLE, now=NOW)

    disabled = repository.set_user_status(created.user.id, "disabled", now=NOW)
    assert disabled.status == "disabled"
    assert disabled.updated_at == created.user.updated_at

    restored = repository.set_user_status(created.user.id, "active", now=NOW + timedelta(hours=1))
    assert restored.status == "active"
    assert restored.updated_at == "2026-10-16T08:30:00.123Z"

    # 再用更早的时间也必须保持「不回退」。
    older = repository.set_user_status(created.user.id, "disabled", now=NOW)
    assert older.updated_at == "2026-10-16T08:30:00.123Z"


def test_set_user_status_rejects_unknown_user_and_bad_arguments(wired) -> None:
    """用户不存在是 404；id 格式非法与非法状态值是 400。"""
    repository, _ = wired
    with pytest.raises(ApplicationError) as missing:
        repository.set_user_status("99999999-9999-9999-9999-999999999999", "disabled", now=NOW)
    assert missing.value.code == "USER_NOT_FOUND"
    assert missing.value.status_code == 404

    created = repository.find_or_create_user_for_identity(GOOGLE, now=NOW)
    with pytest.raises(ApplicationError) as bad_status:
        repository.set_user_status(created.user.id, "suspended", now=NOW)  # type: ignore[arg-type]
    assert bad_status.value.code == "INVALID_ARGUMENT"

    with pytest.raises(ApplicationError) as bad_id:
        repository.set_user_status("nope", "disabled", now=NOW)
    assert bad_id.value.code == "INVALID_ARGUMENT"


def test_list_users_paginates_with_stable_order(wired) -> None:
    """分页必须按 (created_at, id) 稳定排序，同毫秒创建的多个用户也不能漏读。"""
    repository, _ = wired
    identities = [
        LoginIdentity(provider="google", subject=f"subject-{index}", email=None, display_name=f"U{index}")
        for index in range(5)
    ]
    for index, identity in enumerate(identities):
        # 前两个用户同一毫秒创建：只按 created_at 排序时顺序不稳定。
        stamp = NOW if index < 2 else NOW + timedelta(milliseconds=index)
        repository.find_or_create_user_for_identity(identity, now=stamp)

    everything = repository.list_users(limit=MAX_USER_PAGE_SIZE, offset=0)
    assert len(everything) == 5
    assert len({user.id for user in everything}) == 5

    first_page = repository.list_users(limit=2, offset=0)
    second_page = repository.list_users(limit=2, offset=2)
    assert [user.id for user in first_page + second_page] == [
        user.id for user in everything[:4]
    ]
    assert repository.list_users(limit=10, offset=99) == ()


def test_list_users_rejects_out_of_range_paging(wired) -> None:
    """越界分页参数必须抛错，绝不静默截断成"完整结果"。"""
    repository, _ = wired
    for limit in (0, -1, MAX_USER_PAGE_SIZE + 1):
        with pytest.raises(ApplicationError) as error:
            repository.list_users(limit=limit, offset=0)
        assert error.value.code == "INVALID_ARGUMENT"
    with pytest.raises(ApplicationError):
        repository.list_users(limit=1, offset=-1)


def test_count_active_admins_excludes_disabled(wired) -> None:
    """最后管理员保护的判据只统计仍然活动的管理员。"""
    repository, _ = wired
    assert repository.count_active_admins() == 0

    admin = repository.create_admin(GOOGLE, now=NOW)
    repository.find_or_create_user_for_identity(GITHUB, now=NOW)
    assert repository.count_active_admins() == 1

    repository.set_user_status(admin.user.id, "disabled", now=NOW)
    assert repository.count_active_admins() == 0


def test_create_admin_creates_admin_role(wired) -> None:
    """离线初始化必须创建固定 admin 角色，且返回 created=True。"""
    repository, connection = wired

    resolved = repository.create_admin(GOOGLE, now=NOW)

    assert resolved.created is True
    assert resolved.user.role == "admin"
    assert _count(connection, "users") == 1


def test_create_admin_fails_explicitly_for_existing_identity(wired) -> None:
    """同一身份已存在用户时必须明确失败，而不是静默返回既有用户充当初始化成功。"""
    repository, connection = wired
    first = repository.find_or_create_user_for_identity(GOOGLE, now=NOW)

    with pytest.raises(ApplicationError) as error:
        repository.create_admin(GOOGLE, now=NOW)

    assert error.value.code == "USER_IDENTITY_EXISTS"
    assert error.value.status_code == 409
    assert error.value.details == {
        "userId": first.user.id,
        "role": "member",
        "status": "active",
    }
    # 失败不得留下任何痕迹：既没有第二个用户，也没有第二个身份。
    assert _count(connection, "users") == 1
    assert _count(connection, "user_identities") == 1


def test_concurrent_create_converges_to_existing_owner(wired, monkeypatch) -> None:
    """被并发事务抢先时必须删掉候选用户并复用既有归属，created=False。

    用 monkeypatch 让快路径与事务内重查都"看不到"已提交的归属，从而确定性地走到
    ``ON CONFLICT ... DO NOTHING RETURNING`` 返回空结果的那条分支——真实并发无法在
    单进程测试里稳定复现。
    """
    repository, connection = wired
    existing = repository.find_or_create_user_for_identity(GOOGLE, now=NOW)

    original = repository._load_identity  # noqa: SLF001 - 测试刻意的白盒替身
    calls = {"count": 0}

    def blinded(provider: str, subject: str):
        calls["count"] += 1
        if calls["count"] <= 2:
            return None
        return original(provider, subject)

    monkeypatch.setattr(repository, "_load_identity", blinded)

    resolved = repository.find_or_create_user_for_identity(GOOGLE, now=NOW)

    assert resolved.created is False
    assert resolved.user.id == existing.user.id
    assert calls["count"] == 3
    # 候选用户必须已被删除：只剩下原有那一个。
    assert _count(connection, "users") == 1
    assert _count(connection, "user_identities") == 1


def test_concurrent_create_raises_when_ownership_cannot_be_read_back(
    wired, monkeypatch
) -> None:
    """抢先者提交后又读不回归属时，必须显式失败并回滚，不得返回半成品。"""
    repository, connection = wired
    repository.find_or_create_user_for_identity(GOOGLE, now=NOW)

    monkeypatch.setattr(repository, "_load_identity", lambda provider, subject: None)

    with pytest.raises(ApplicationError) as error:
        repository.find_or_create_user_for_identity(GOOGLE, now=NOW)

    assert error.value.code == "USER_IDENTITY_CONFLICT"
    assert error.value.retryable is True
    # 事务整体回滚：候选用户不存在，身份数也没有变化。
    assert _count(connection, "users") == 1
    assert _count(connection, "user_identities") == 1


def test_create_admin_converges_without_leaking_candidate_user(wired, monkeypatch) -> None:
    """create_admin 撞并发时同样不能残留候选用户。"""
    repository, connection = wired
    existing = repository.find_or_create_user_for_identity(GOOGLE, now=NOW)

    original = repository._load_identity  # noqa: SLF001 - 测试刻意的白盒替身
    calls = {"count": 0}

    def blinded(provider: str, subject: str):
        calls["count"] += 1
        if calls["count"] <= 1:
            return None
        return original(provider, subject)

    monkeypatch.setattr(repository, "_load_identity", blinded)

    with pytest.raises(ApplicationError) as error:
        repository.create_admin(GOOGLE, now=NOW)

    assert error.value.code == "USER_IDENTITY_EXISTS"
    assert error.value.details is not None
    assert error.value.details["userId"] == existing.user.id
    assert _count(connection, "users") == 1


def test_transaction_rolls_back_user_and_identity_together(wired) -> None:
    """失败时不得残留「只有用户、没有身份」的半成品。"""
    repository, connection = wired

    with pytest.raises(RuntimeError):
        with repository.transaction():
            repository.create_admin(GOOGLE, now=NOW)
            raise RuntimeError("模拟写入后失败")

    assert _count(connection, "users") == 0
    assert _count(connection, "user_identities") == 0


def test_transaction_is_reentrant_across_repository_instances(wired) -> None:
    """登录链路要把多个写入合成一个事务，嵌套调用不得重复 BEGIN。

    DuckDB 在事务内再执行 BEGIN 会报错**并中止外层事务**，所以这里既验证不抛错，
    也验证外层事务仍然真的在起作用。
    """
    repository, connection = wired
    other = UserRepository(connection)

    with repository.transaction():
        created = other.create_admin(GOOGLE, now=NOW)
        other.find_or_create_user_for_identity(GITHUB, now=NOW)
        repository.set_user_status(created.user.id, "disabled", now=NOW)

    assert repository.find_user_by_id(created.user.id).status == "disabled"
    assert _count(connection, "users") == 2


def test_outer_transaction_rolls_back_all_nested_writes(wired) -> None:
    """外层事务里任一写入失败，之前已成功的嵌套写入也必须一起回滚。"""
    repository, connection = wired

    with pytest.raises(ApplicationError):
        with repository.transaction():
            repository.create_admin(GOOGLE, now=NOW)
            repository.set_user_status(
                "99999999-9999-9999-9999-999999999999", "disabled", now=NOW
            )

    assert _count(connection, "users") == 0
    assert _count(connection, "user_identities") == 0


def test_new_user_never_inherits_role_or_ledger(wired) -> None:
    """自动注册只创建 member 与一条身份，不附带任何其他资源。"""
    repository, connection = wired
    repository.create_admin(GOOGLE, now=NOW)

    newcomer = repository.find_or_create_user_for_identity(GITHUB, now=NOW)

    assert newcomer.user.role == "member"
    assert _count(connection, "accounts") == 0
    assert _count(connection, "ledger_versions") == 0
    assert _count(connection, "user_identities") == 2
