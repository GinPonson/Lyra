"""迁移清单、SQL 与「接管既有账本」的验证。

本文件的核心主张只有一条：**Python 能无损打开既有的 schema 15 库。**

证据是一份**冻结的夹具** ``fixtures/typescript-ledger-schema15.duckdb``——它由迁移前的
TypeScript 服务端创建（schema 15、28 张表、无数据），因此打开它验证的是"接管一个不是
Python 造的库"，而不是"自己造自己读"。

**它为什么是夹具而不是实时读取**：迁移完成后 TypeScript 服务端已移除，实时读取会让本
测试随它一起消失。夹具保留了这份证据，且**来源可追溯**（见 `fixtures/README.md`）。

SQL 副本现在是**唯一来源**——不再与另一个实现逐字节对照。

v16 起多了一类需要外部输入才能完成的迁移：把旧库的全部资源归给一个**显式确认**的
原用户。本文件只验证这条迁移的**入口契约**（缺少归属必须明确失败、上下文不得留在库里），
升级后的完整结构断言在 `test_multi_user_schema.py`。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
import pytest

from lyra.db.connection import open_lyra_database
from lyra.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    MIGRATION_CONTEXT_TABLE,
    MIGRATION_CONTEXT_VERSION,
    MigrationContext,
    MigrationError,
    MigrationOwnerIdentity,
    assert_migration_history,
    apply_migrations,
)

PYTHON_SQL_DIRECTORY = Path(__file__).resolve().parents[1] / "src" / "lyra" / "db" / "sql"

# **冻结夹具**：迁移前由 TypeScript 服务端创建，schema 15、28 张表、无数据。
# 不含任何账户、交易或设备记录，因此可以签入仓库。
FIXTURE_DATABASE = (
    Path(__file__).resolve().parent / "fixtures" / "typescript-ledger-schema15.duckdb"
)


def _copy_fixture_database(destination: Path) -> None:
    """把冻结夹具复制到临时目录。

    **必须先复制再打开**：DuckDB 同一时刻只允许一个进程打开一个文件，而直接打开夹具
    会让并行测试相互冲突。
    """
    shutil.copy2(FIXTURE_DATABASE, destination)


def test_migration_manifest_matches_current_schema_version() -> None:
    """清单最后一条必须等于代码声明的 schema 版本。"""
    assert MIGRATIONS[-1].version == CURRENT_SCHEMA_VERSION
    assert [migration.version for migration in MIGRATIONS] == list(
        range(1, CURRENT_SCHEMA_VERSION + 1)
    )


def test_every_migration_has_a_sql_file() -> None:
    """**清单里的每一条迁移都必须有对应的 SQL 文件，且非空。**

    这条测试此前是"与 TypeScript 源逐字节对照"——那份耦合已随 TS 服务端移除。SQL 副本
    现在是唯一来源，因此检查的重点从"与谁一致"变成"**自身完整**"：清单声明了 N 条迁移，
    磁盘上就必须有 N 个非空文件，否则迁移会在运行时才失败。
    """
    for migration in MIGRATIONS:
        path = PYTHON_SQL_DIRECTORY / migration.file_name
        assert path.is_file(), f"缺少迁移 SQL：{migration.file_name}"
        assert path.stat().st_size > 0, f"迁移 SQL 为空：{migration.file_name}"


def test_migration_sixteen_is_the_owner_context_migration() -> None:
    """v16 是**唯一**需要显式归属确认的迁移，但它不必是清单最后一条。

    这条测试原先断言"需要上下文的迁移就是最后一条"。v17（邀请码）出现后那个等式
    不再成立，而**要保护的意图没变**：需要上下文的是且仅是 v16——新增迁移不该顺手
    要求归属输入，否则每加一张表都会强迫操作者在升级时重新确认一遍 owner。
    """
    assert MIGRATION_CONTEXT_VERSION == 16
    context_migrations = [m for m in MIGRATIONS if m.version == MIGRATION_CONTEXT_VERSION]
    assert len(context_migrations) == 1
    assert context_migrations[0].file_name == "multi-user-schema.sql"
    assert [m.version for m in MIGRATIONS] == list(range(1, CURRENT_SCHEMA_VERSION + 1))
    assert MIGRATIONS[-1].version == CURRENT_SCHEMA_VERSION == 17
    assert MIGRATIONS[-1].file_name == "invite-code-schema.sql"


def _populated_schema15_database(destination: Path) -> None:
    """复制冻结夹具并放入一行账户，构造「非空但没有归属信息」的旧库。"""
    _copy_fixture_database(destination)
    connection = duckdb.connect(str(destination))
    try:
        connection.execute(
            """
            INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
            VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'Main', 'ACC-1', '1234')
            """
        )
    finally:
        connection.close()


def _owner_context() -> MigrationContext:
    """最小可用的显式归属确认。"""
    return MigrationContext(
        owner_user_id="11111111-1111-1111-1111-111111111111",
        owner_display_name="Owner",
        identities=(
            MigrationOwnerIdentity(
                provider="google",
                subject="112233",
                email="owner@example.com",
                display_name="Owner",
                is_device_identity=True,
            ),
        ),
    )


@pytest.mark.skipif(not FIXTURE_DATABASE.is_file(), reason="缺少冻结的既有账本夹具")
def test_populated_database_without_owner_context_is_rejected(tmp_path: Path) -> None:
    """**库非空却没有显式归属时必须明确失败。**

    这是计划 §9.3 的硬要求：旧数据只能归给经人工确认的原 owner，绝不能落到某个默认
    用户或首次登录者身上。失败后旧库必须原封不动。
    """
    target = tmp_path / "no-context.duckdb"
    _populated_schema15_database(target)

    connection = duckdb.connect(str(target))
    try:
        with pytest.raises(MigrationError, match="必须显式提供原 owner 归属确认"):
            apply_migrations(connection)
        # 失败即回滚：旧表仍在、仍是 schema 15、没有任何用户被创建。
        assert connection.execute("SELECT count(*) FROM accounts").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'accounts' AND column_name = 'user_id'"
        ).fetchone() == (0,)
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone() == (15,)
        assert connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'users'"
        ).fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.skipif(not FIXTURE_DATABASE.is_file(), reason="缺少冻结的既有账本夹具")
def test_owner_context_is_consumed_and_not_left_behind(tmp_path: Path) -> None:
    """迁移上下文只服务于这一次升级，不得成为运行时可读的长期归属来源。"""
    target = tmp_path / "context.duckdb"
    _populated_schema15_database(target)

    connection = duckdb.connect(str(target))
    try:
        apply_migrations(connection, _owner_context())

        assert connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [MIGRATION_CONTEXT_TABLE],
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT max(version) FROM schema_migrations"
        ).fetchone() == (CURRENT_SCHEMA_VERSION,)
        owner = connection.execute("SELECT id, role FROM users").fetchall()
        assert len(owner) == 1 and owner[0][1] == "admin"
        # 再次调用（模拟重启）不得重复迁移或新建用户。
        apply_migrations(connection, _owner_context())
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (1,)
    finally:
        connection.close()


def test_opens_and_migrates_fresh_database(tmp_path: Path) -> None:
    """全新库应能从零迁移到当前 schema 版本。"""
    database = open_lyra_database(str(tmp_path / "fresh.duckdb"))
    try:
        health = database.read_health()
        assert health["status"] == "available"
        assert health["schemaVersion"] == CURRENT_SCHEMA_VERSION
        # **健康响应不再带账本版本**：它是全库 max(version)，多用户下属于跨用户活动量，
        # 公开端点不得暴露（计划 §4.4）。空账本版本为 0 这件事改由按用户的业务响应断言。
        assert "ledgerVersion" not in health
    finally:
        database.close()


@pytest.mark.skipif(not FIXTURE_DATABASE.is_file(), reason="缺少冻结的既有账本夹具")
def test_takes_over_typescript_created_database(tmp_path: Path) -> None:
    """接管由 TypeScript 服务端创建的 schema 15 账本，且不改变其内容。

    v16 会**新增** ``users`` 与 ``user_identities`` 两张表，因此「表集合完全不变」不再是
    正确的期望。这里把断言收紧成精确等式：接管后必须**恰好**多出这两张表、一张不少，
    而且它们必须是空的——夹具本身没有业务数据，所以接管不得凭空造出任何用户。
    """
    target = tmp_path / "lyra.duckdb"
    _copy_fixture_database(target)

    before = duckdb.connect(str(target), read_only=True)
    try:
        before_tables = sorted(
            row[0]
            for row in before.execute(
                "SELECT table_name FROM information_schema.tables ORDER BY table_name"
            ).fetchall()
        )
    finally:
        before.close()
    assert before_tables, "TypeScript 账本里没有任何表"

    database = open_lyra_database(str(target))
    try:
        health = database.read_health()
        assert health["status"] == "available"
        assert health["schemaVersion"] == CURRENT_SCHEMA_VERSION
        # 迁移历史必须与 Python 清单逐项一致，否则说明两侧清单已经漂移。
        assert_migration_history(database.connection)

        after_tables = sorted(
            row[0]
            for row in database.connection.execute(
                "SELECT table_name FROM information_schema.tables ORDER BY table_name"
            ).fetchall()
        )
        # v16 必须删除旧 owner 身份表：schema 16 的归属已经落在 users/user_identities 上，
        # 留着旧表等于留下**第二条可读的授权来源**（计划 §5 P1b）。
        assert "server_owner_identity" in before_tables, (
            "冻结夹具里应当有旧 owner 身份表——否则这条断言测不到删除行为"
        )
        assert "server_owner_identity" not in after_tables, (
            "v16 必须删除旧 owner 身份表，但接管后它仍然存在"
        )
        assert after_tables == sorted(
            [table for table in before_tables if table != "server_owner_identity"]
            + ["invite_codes", "user_identities", "users"]
        ), "接管后表集合与「原表 − 旧 owner 身份表 + v16/v17 新表」不一致"
        # 没有业务数据的旧库不需要归属输入，也不得被凭空分配一个默认用户。
        for table in ("users", "user_identities"):
            assert database.connection.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone() == (0,)
    finally:
        database.close()


def test_rejects_database_with_future_schema(tmp_path: Path) -> None:
    """迁移历史多于服务端清单时必须拒绝打开（旧服务不得打开未来数据库）。"""
    path = tmp_path / "future.duckdb"
    database = open_lyra_database(str(path))
    database.connection.execute(
        "INSERT INTO schema_migrations (version, description) VALUES (999, 'future migration')"
    )
    database.close()

    with pytest.raises(MigrationError, match="数量与服务端不一致"):
        open_lyra_database(str(path))


def test_rejects_database_with_mismatched_description(tmp_path: Path) -> None:
    """描述被篡改时必须拒绝，避免「版本号对得上但内容不是同一迁移」。"""
    path = tmp_path / "tampered.duckdb"
    database = open_lyra_database(str(path))
    database.connection.execute(
        "UPDATE schema_migrations SET description = 'tampered' WHERE version = 1"
    )
    database.close()

    with pytest.raises(MigrationError, match="与服务端不一致"):
        open_lyra_database(str(path))


def test_temp_directory_is_sibling_of_database(tmp_path: Path) -> None:
    """溢出目录必须固定在账本同级，容器只读根文件系统下才可用。"""
    database = open_lyra_database(str(tmp_path / "ledger.duckdb"))
    try:
        row = database.connection.execute("SELECT current_setting('temp_directory')").fetchone()
    finally:
        database.close()
    assert row is not None
    assert Path(str(row[0])).name == "tmp"
    assert Path(str(row[0])).parent == tmp_path


def test_empty_database_path_is_rejected() -> None:
    """空路径必须直接拒绝，不得静默落到某个默认位置。"""
    with pytest.raises(ValueError, match="不能为空"):
        open_lyra_database("   ")
