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

**迁移上下文**（v16 起）：多用户改造要把旧库里的全部资源归给一个**显式确认**的
原用户。调用方通过 :class:`MigrationContext` 传入这份确认，迁移 SQL 从一张临时表
``_migration_context`` 读取它。库非空却没有上下文时，迁移**明确失败**——绝不把旧
数据分配给某个默认用户或首次登录者（计划 §9.3）。全新空库不需要上下文。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import duckdb

# 当前服务端代码能够迁移并读取的最新 DuckDB schema 版本。
CURRENT_SCHEMA_VERSION = 16

# 需要迁移上下文才能回填归属的那一条迁移。只有它读取 ``_migration_context``。
MIGRATION_CONTEXT_VERSION = 16

# 迁移 SQL 读取的上下文临时表名；由 :func:`apply_migrations` 创建、填充并清理。
MIGRATION_CONTEXT_TABLE = "_migration_context"

_SQL_DIRECTORY = Path(__file__).parent / "sql"

# **非空判定表清单**：这些表在 v16 中必须回填 ``user_id``（或改挂到用户主键上），
# 因此它们只要有数据，就说明旧库确实需要一份显式归属确认。其余表（交易、FIFO、
# 已实现、提醒、设备凭据、Agent 消息等）经 account 或设备间接归属，不参与判定。
_CONTEXT_REQUIRED_TABLES: tuple[str, ...] = (
    "accounts",
    "paired_devices",
    "import_files",
    "ledger_versions",
    "agent_session_identities",
    "gmail_connections",
    "market_source_settings",
)

# 上下文表结构。迁移 SQL 只读它，建表与填充都由这里负责，避免两处 DDL 漂移。
_MIGRATION_CONTEXT_DDL = f"""
CREATE TEMP TABLE {MIGRATION_CONTEXT_TABLE} (
    owner_user_id      UUID NOT NULL,
    owner_display_name VARCHAR NOT NULL,
    owner_status       VARCHAR NOT NULL,
    owner_role         VARCHAR NOT NULL,
    provider           VARCHAR NOT NULL,
    subject            VARCHAR NOT NULL,
    email              VARCHAR,
    display_name       VARCHAR,
    is_device_identity BOOLEAN NOT NULL
)
"""


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
    Migration(
        16,
        "per-user ledger ownership and login identities",
        "multi-user-schema.sql",
    ),
)


@dataclass(frozen=True)
class MigrationOwnerIdentity:
    """一次升级中**显式确认**归属于原 owner 的一条来源身份。

    邮箱与昵称只作展示，身份键始终是 ``(provider, subject)``；未确认的历史身份
    不得借迁移自动获得原账本（计划 §9.3）。

    @param provider: 登录来源标识，如 ``google`` / ``linuxdo`` / ``github``。
    @param subject: 该来源下已验证的不可变主体标识。
    @param email: 可选展示邮箱；不参与身份关联。
    @param display_name: 可选展示名。
    @param is_device_identity: 是否作为**既有设备**的身份来源。旧库没有记录设备本次
        登录用的是哪个身份，因此必须显式指定，不能由迁移自行猜测。
    """

    provider: str
    subject: str
    email: str | None = None
    display_name: str | None = None
    is_device_identity: bool = False


@dataclass(frozen=True)
class MigrationContext:
    """schema 15 → 16 一次性升级所需的显式归属确认。

    这是**一次升级的输入**，不是长期的 owner 兼容模式：迁移完成后运行时不再读取它。

    @param owner_user_id: 旧库全部资源要归属到的用户 UUID（文本形式）。
    @param owner_display_name: 该用户的显示名称，非空。
    @param identities: 已确认归属于该用户的全部来源身份，至少一条。
    @param owner_status: 初始状态，``active`` 或 ``disabled``。
    @param owner_role: 初始角色，``admin`` 或 ``member``。
    """

    owner_user_id: str
    owner_display_name: str
    identities: tuple[MigrationOwnerIdentity, ...]
    owner_status: str = "active"
    owner_role: str = "admin"


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


def apply_migrations(
    connection: duckdb.DuckDBPyConnection,
    context: MigrationContext | None = None,
) -> None:
    """在同一事务中执行全部幂等迁移，并校验迁移历史。

    @param connection: 已打开的 DuckDB 连接。
    @param context: 需要回填用户归属的迁移（v16）所依赖的显式归属确认。全新空库可以
        不传；旧库有业务数据却不传时**明确失败**，不会把数据分配给默认用户。
    @raises MigrationError: 清单与代码版本不一致、上下文缺失或非法、迁移失败、历史校验不通过。
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
            if migration.version in applied:
                continue
            # 上下文必须在执行该条 SQL 之前就绪：这条迁移的 SQL 会从上下文表读归属。
            if migration.version == MIGRATION_CONTEXT_VERSION:
                _prepare_migration_context(connection, context)
            connection.execute(sql)
        # 上下文只服务于本次升级，绝不留下可供运行时读取的归属来源。
        _drop_migration_context(connection)
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


def _prepare_migration_context(
    connection: duckdb.DuckDBPyConnection,
    context: MigrationContext | None,
) -> None:
    """在执行 v16 之前准备归属上下文临时表。

    @param connection: 处于迁移事务内的连接。
    @param context: 调用方给出的显式归属确认；``None`` 表示未提供。
    @raises MigrationError: 旧库有需要归属的数据却未提供上下文，或上下文本身非法。
    """
    if context is None:
        non_empty = [
            table for table in _CONTEXT_REQUIRED_TABLES if _table_has_rows(connection, table)
        ]
        if non_empty:
            raise MigrationError(
                "schema 15 库已存在业务数据，必须显式提供原 owner 归属确认"
                f"（MigrationContext）；拒绝为 {', '.join(non_empty)} 中的既有数据"
                "猜测归属。请先用离线升级入口确认原 owner 的用户与身份。"
            )
        # 空库也建表：v16 的 SQL 无条件从上下文表读归属，空表会让标量子查询得到 NULL，
        # 而暂存表为空时 0 行插入正是全新库的正确结果。
        connection.execute(_MIGRATION_CONTEXT_DDL)
        return

    _assert_migration_context_valid(connection, context)
    connection.execute(_MIGRATION_CONTEXT_DDL)
    connection.executemany(
        f"INSERT INTO {MIGRATION_CONTEXT_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                context.owner_user_id,
                context.owner_display_name,
                context.owner_status,
                context.owner_role,
                identity.provider,
                identity.subject,
                identity.email,
                identity.display_name,
                identity.is_device_identity,
            )
            for identity in context.identities
        ],
    )


def _assert_migration_context_valid(
    connection: duckdb.DuckDBPyConnection,
    context: MigrationContext,
) -> None:
    """校验上下文可安全用作归属来源；任何歧义都必须在这里暴露。

    @param connection: 处于迁移事务内的连接（用于判断是否真的存在既有设备）。
    @param context: 待校验的上下文。
    @raises MigrationError: 用户 id 非法、显示名为空、状态或角色越界、身份为空或重复、
        既有设备存在时未指定唯一的设备身份来源。
    """
    try:
        UUID(context.owner_user_id)
    except ValueError as error:
        raise MigrationError(
            f"迁移上下文的 owner_user_id 不是合法 UUID: {context.owner_user_id!r}"
        ) from error
    if not context.owner_display_name.strip():
        raise MigrationError("迁移上下文的 owner_display_name 不能为空")
    if context.owner_status not in ("active", "disabled"):
        raise MigrationError(f"迁移上下文的 owner_status 非法: {context.owner_status!r}")
    if context.owner_role not in ("admin", "member"):
        raise MigrationError(f"迁移上下文的 owner_role 非法: {context.owner_role!r}")
    if not context.identities:
        raise MigrationError("迁移上下文必须至少包含一条已确认身份")
    seen: set[tuple[str, str]] = set()
    for identity in context.identities:
        if not identity.provider.strip() or not identity.subject.strip():
            raise MigrationError("迁移上下文的身份必须同时具有 provider 与 subject")
        key = (identity.provider, identity.subject)
        if key in seen:
            raise MigrationError(f"迁移上下文包含重复身份: {identity.provider}/{identity.subject}")
        seen.add(key)

    device_identities = [item for item in context.identities if item.is_device_identity]
    # 旧设备必须能唯一确定「本次登录用的是哪个身份」。有设备却指定了 0 条或 2 条以上
    # 就说明操作者没给出明确归属，此时失败远好于随便挑一条。
    if _table_has_rows(connection, "paired_devices") and len(device_identities) != 1:
        raise MigrationError(
            "旧库存在既有设备，迁移上下文必须且只能指定一条 is_device_identity=True "
            f"的身份作为设备身份来源，实际指定 {len(device_identities)} 条"
        )
    if len(device_identities) > 1:
        raise MigrationError("迁移上下文最多只能指定一条 is_device_identity=True 的身份")


def _table_has_rows(connection: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    """判断指定表是否存在且至少有一行。

    **表不存在按空处理**：全新库在跑到 v16 时前面的迁移已建好全部表，但判定逻辑
    不应因为某个历史迁移尚未执行而误报"非空"。

    @param connection: 数据库连接。
    @param table_name: 表名（仅取自本模块内的常量清单）。
    @returns 表存在且有数据时为 ``True``。
    """
    exists = connection.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
        [table_name],
    ).fetchone()
    if exists is None or int(exists[0]) == 0:
        return False
    return connection.execute(f"SELECT 1 FROM {table_name} LIMIT 1").fetchone() is not None


def _drop_migration_context(connection: duckdb.DuckDBPyConnection) -> None:
    """删除上下文临时表，避免它成为运行时可读的归属来源。

    @param connection: 数据库连接；表不存在时为无操作。
    """
    connection.execute(f"DROP TABLE IF EXISTS {MIGRATION_CONTEXT_TABLE}")



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
