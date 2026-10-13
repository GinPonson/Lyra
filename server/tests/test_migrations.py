"""迁移清单、SQL 与「接管既有账本」的验证。

本文件的核心主张只有一条：**Python 能无损打开既有的 schema 15 库。**

证据是一份**冻结的夹具** ``fixtures/typescript-ledger-schema15.duckdb``——它由迁移前的
TypeScript 服务端创建（schema 15、28 张表、无数据），因此打开它验证的是"接管一个不是
Python 造的库"，而不是"自己造自己读"。

**它为什么是夹具而不是实时读取**：迁移完成后 TypeScript 服务端已移除，实时读取会让本
测试随它一起消失。夹具保留了这份证据，且**来源可追溯**（见 `fixtures/README.md`）。

SQL 副本现在是**唯一来源**——不再与另一个实现逐字节对照。
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
    MigrationError,
    assert_migration_history,
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


def test_opens_and_migrates_fresh_database(tmp_path: Path) -> None:
    """全新库应能从零迁移到当前 schema 版本。"""
    database = open_lyra_database(str(tmp_path / "fresh.duckdb"))
    try:
        health = database.read_health()
        assert health["status"] == "available"
        assert health["schemaVersion"] == CURRENT_SCHEMA_VERSION
        # 空账本的账本版本为 0。
        assert health["ledgerVersion"] == 0
    finally:
        database.close()


@pytest.mark.skipif(not FIXTURE_DATABASE.is_file(), reason="缺少冻结的既有账本夹具")
def test_takes_over_typescript_created_database(tmp_path: Path) -> None:
    """接管由 TypeScript 服务端创建的 schema 15 账本，且不改变其内容。"""
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
        assert after_tables == before_tables, "接管后表集合发生了变化"
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
