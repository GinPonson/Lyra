"""DuckDB schema 迁移。

镜像 TypeScript 版 ``database.ts`` 的 ``applyMigrations``，语义逐项对齐：

1. 迁移清单必须与代码声明的 schema 版本一致；
2. 整个迁移在一个事务内完成，失败即回滚；
3. 第 1 条（bootstrap）**每次启动都执行**（其 SQL 幂等），用于创建迁移表；
4. 其余版本只在 ``schema_migrations`` 中**尚未记录**时执行；
5. 迁移历史必须与服务端清单**逐项一致**，从而拒绝旧服务打开未来的数据库；
6. 成功后立即 ``CHECKPOINT``，避免运行期继续携带整批 DDL 的 WAL。

第 3 条尤其不能改成"只跑一次"：需要重建表的迁移（如 v11）在每次启动重复
执行会报错，而 bootstrap 的幂等 DDL 既安全又能保证全新库可用。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

# 当前服务端代码能够迁移并读取的最新 DuckDB schema 版本。
CURRENT_SCHEMA_VERSION = 15

_SQL_DIRECTORY = Path(__file__).parent / "sql"


@dataclass(frozen=True)
class Migration:
    """一条按依赖顺序执行的迁移。"""

    version: int
    description: str
    file_name: str


# 按依赖顺序执行的完整迁移清单；必须与 TypeScript 版 MIGRATIONS 完全一致。
MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "phase 1 ledger core", "schema.sql"),
    Migration(2, "gmail connections and message metadata", "gmail-schema.sql"),
    Migration(3, "paired devices and refresh credentials", "auth-schema.sql"),
    Migration(4, "security and business audit events", "audit-schema.sql"),
    Migration(5, "normalized agent sessions and tool calls", "agent-schema.sql"),
    Migration(6, "short-lived device access tokens", "access-token-schema.sql"),
    Migration(7, "short-lived import preview authorization metadata", "import-preview-schema.sql"),
    Migration(8, "per-account official import reminder settings", "reconciliation-reminder-schema.sql"),
    Migration(9, "cashflow transaction provenance view", "transaction-source-view-schema.sql"),
    Migration(10, "device-scoped mutation idempotency records", "idempotency-schema.sql"),
    Migration(11, "immutable agent session identity for mutable lifecycle", "agent-session-identity-schema.sql"),
    Migration(12, "single Genkit runtime and archived legacy sessions", "agent-genkit-schema.sql"),
    Migration(13, "single owner Google login and credential reauthorization", "owner-login-schema.sql"),
    Migration(14, "owner-wide market source preferences", "market-settings-schema.sql"),
    Migration(15, "multiple configured OAuth identities for the private owner", "owner-login-providers-schema.sql"),
)


class MigrationError(RuntimeError):
    """迁移清单、历史或执行过程与预期不符。"""


def load_migration_sql(migration: Migration) -> str:
    """读取一条迁移的 SQL 脚本。

    @param migration: 迁移清单项。
    @returns SQL 文本。
    @throws MigrationError: 脚本文件缺失。
    """
    path = _SQL_DIRECTORY / migration.file_name
    if not path.is_file():
        raise MigrationError(f"缺少迁移脚本: {migration.file_name}")
    return path.read_text(encoding="utf-8")


def apply_migrations(connection: duckdb.DuckDBPyConnection) -> None:
    """在同一事务中执行全部幂等迁移，并校验迁移历史。

    @param connection: 已打开的 DuckDB 连接。
    @raises MigrationError: 清单与代码版本不一致、迁移失败或历史校验不通过。
    """
    if MIGRATIONS[-1].version != CURRENT_SCHEMA_VERSION:
        raise MigrationError("DuckDB 迁移清单与当前 schema 版本不一致")

    scripts = [(migration, load_migration_sql(migration)) for migration in MIGRATIONS]
    bootstrap = scripts[0]

    connection.execute("BEGIN TRANSACTION")
    try:
        # bootstrap 每次启动都执行：它创建 schema_migrations 并保持幂等。
        connection.execute(bootstrap[1])
        applied = {
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }
        for migration, sql in scripts[1:]:
            if migration.version not in applied:
                connection.execute(sql)
        assert_migration_history(connection)
        connection.execute("COMMIT")
    except Exception as error:
        try:
            connection.execute("ROLLBACK")
        except Exception as rollback_error:  # noqa: BLE001 - 回滚失败必须一并暴露
            raise MigrationError(
                f"迁移失败且回滚失败: {error!r} / 回滚错误: {rollback_error!r}"
            ) from error
        raise
    # DuckDB 在 schema WAL 被强制中断后可能无法回放；迁移成功后立即建立检查点。
    connection.execute("CHECKPOINT")


def assert_migration_history(connection: duckdb.DuckDBPyConnection) -> None:
    """迁移表必须与代码清单逐项一致，禁止旧服务打开未来数据库。

    @param connection: 已打开的 DuckDB 连接。
    @raises MigrationError: 数量或任一条的版本、描述与清单不符。
    """
    rows = connection.execute(
        "SELECT version, description FROM schema_migrations ORDER BY version"
    ).fetchall()
    if len(rows) != len(MIGRATIONS):
        raise MigrationError("DuckDB schema migration 数量与服务端不一致")
    for row, migration in zip(rows, MIGRATIONS, strict=True):
        if int(row[0]) != migration.version or row[1] != migration.description:
            raise MigrationError(f"DuckDB schema migration v{migration.version} 与服务端不一致")
