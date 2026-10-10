import { readFile } from "node:fs/promises";

import {
  DuckDBConnection,
  DuckDBInstance,
} from "@duckdb/node-api";

import type { DatabaseHealth } from "../../application/index.js";
import { CURRENT_SCHEMA_VERSION } from "./schema-version.js";

/** 按依赖顺序执行的完整迁移清单。 */
const MIGRATIONS = [
  { version: 1, description: "phase 1 ledger core", fileName: "schema.sql" },
  { version: 2, description: "gmail connections and message metadata", fileName: "gmail-schema.sql" },
  { version: 3, description: "paired devices and refresh credentials", fileName: "auth-schema.sql" },
  { version: 4, description: "security and business audit events", fileName: "audit-schema.sql" },
  { version: 5, description: "normalized agent sessions and tool calls", fileName: "agent-schema.sql" },
  { version: 6, description: "short-lived device access tokens", fileName: "access-token-schema.sql" },
  { version: 7, description: "short-lived import preview authorization metadata", fileName: "import-preview-schema.sql" },
  { version: 8, description: "per-account official import reminder settings", fileName: "reconciliation-reminder-schema.sql" },
  { version: 9, description: "cashflow transaction provenance view", fileName: "transaction-source-view-schema.sql" },
  { version: 10, description: "device-scoped mutation idempotency records", fileName: "idempotency-schema.sql" },
  {
    version: 11,
    description: "immutable agent session identity for mutable lifecycle",
    fileName: "agent-session-identity-schema.sql",
  },
  {
    version: 12,
    description: "single Genkit runtime and archived legacy sessions",
    fileName: "agent-genkit-schema.sql",
  },
  {
    version: 13,
    description: "single owner Google login and credential reauthorization",
    fileName: "owner-login-schema.sql",
  },
] as const;

/** 单进程共用的 DuckDB 实例与连接。 */
export class LyraDatabase {
  readonly connection: DuckDBConnection;
  readonly #instance: DuckDBInstance;
  #closed = false;

  /** 仅由 `openLyraDatabase` 在迁移成功后创建。 */
  constructor(instance: DuckDBInstance, connection: DuckDBConnection) {
    this.#instance = instance;
    this.connection = connection;
  }

  /** 读取健康端点需要的实际 schema 与账本版本。 */
  async readHealth(): Promise<DatabaseHealth> {
    if (this.#closed) {
      return { status: "unavailable" };
    }
    const reader = await this.connection.runAndReadAll(`
      SELECT
        (SELECT max(version) FROM schema_migrations) AS schema_version,
        coalesce((SELECT max(version) FROM ledger_versions), 0) AS ledger_version
    `);
    const row = reader.getRowObjectsJson()[0];
    if (row === undefined) {
      throw new Error("DuckDB 健康查询未返回结果");
    }
    const schemaVersion = parseVersion(row.schema_version, "schema version");
    const ledgerVersion = parseVersion(row.ledger_version, "ledger version");
    return { status: "available", schemaVersion, ledgerVersion };
  }

  /** 先断开连接再关闭实例；重复调用保持幂等。 */
  close(): void {
    if (this.#closed) {
      return;
    }
    this.connection.closeSync();
    this.#instance.closeSync();
    this.#closed = true;
  }
}

/**
 * 打开 Lyra DuckDB，并在同一事务中执行和核验 v1–v13 迁移。
 *
 * @param databasePath - 显式数据库文件路径；测试可传 `:memory:`。
 * @returns 已迁移、可供单进程服务使用的数据库连接。
 */
export async function openLyraDatabase(databasePath: string): Promise<LyraDatabase> {
  const normalizedPath = databasePath.trim();
  if (normalizedPath.length === 0) {
    throw new RangeError("DuckDB 路径不能为空");
  }

  const instance = await DuckDBInstance.create(normalizedPath);
  let connection: DuckDBConnection | null = null;
  try {
    connection = await instance.connect();
    await applyMigrations(connection);
    return new LyraDatabase(instance, connection);
  } catch (error: unknown) {
    const cleanupErrors: unknown[] = [];
    if (connection !== null) {
      try {
        connection.closeSync();
      } catch (cleanupError: unknown) {
        cleanupErrors.push(cleanupError);
      }
    }
    try {
      instance.closeSync();
    } catch (cleanupError: unknown) {
      cleanupErrors.push(cleanupError);
    }
    if (cleanupErrors.length > 0) {
      throw new AggregateError([error, ...cleanupErrors], "DuckDB 打开失败且资源清理失败");
    }
    throw error;
  }
}

/** 在一个事务中执行全部幂等迁移，并拒绝版本缺失、描述变化或未来 schema。 */
async function applyMigrations(connection: DuckDBConnection): Promise<void> {
  const latestMigration = MIGRATIONS.at(-1);
  if (latestMigration?.version !== CURRENT_SCHEMA_VERSION) {
    throw new Error("DuckDB 迁移清单与当前 schema 版本不一致");
  }
  const scripts = await Promise.all(MIGRATIONS.map(async (migration) => ({
    ...migration,
    sql: await readFile(new URL(`./${migration.fileName}`, import.meta.url), "utf8"),
  })));

  await connection.run("BEGIN TRANSACTION");
  try {
    const bootstrap = scripts[0];
    if (bootstrap === undefined) {
      throw new Error("DuckDB 迁移清单不能为空");
    }
    // v1 创建迁移表且保持幂等；后续版本只在尚未记录时执行。这样需要重建表的
    // v11 不会在每次启动重复运行，同时仍能从任一旧版本连续升级。
    await connection.run(bootstrap.sql);
    const appliedReader = await connection.runAndReadAll(
      "SELECT version FROM schema_migrations",
    );
    const appliedVersions = new Set(appliedReader.getRowObjectsJson().map((row) => (
      parseVersion(row.version, "migration version")
    )));
    for (const migration of scripts.slice(1)) {
      if (!appliedVersions.has(migration.version)) {
        await connection.run(migration.sql);
      }
    }
    await assertMigrationHistory(connection);
    await connection.run("COMMIT");
  } catch (error: unknown) {
    try {
      await connection.run("ROLLBACK");
    } catch (rollbackError: unknown) {
      throw new AggregateError([error, rollbackError], "DuckDB 迁移失败且回滚失败");
    }
    throw error;
  }
  // DuckDB 1.5.5 在 schema WAL 被进程强制中断后可能无法回放；迁移成功后
  // 立即建立检查点，避免服务正常运行期间继续携带整批 DDL WAL。
  await connection.run("CHECKPOINT");
}

/** 迁移表必须与代码清单逐项一致，禁止旧服务打开未来数据库。 */
async function assertMigrationHistory(connection: DuckDBConnection): Promise<void> {
  const reader = await connection.runAndReadAll(
    "SELECT version, description FROM schema_migrations ORDER BY version",
  );
  const rows = reader.getRowObjectsJson();
  if (rows.length !== MIGRATIONS.length) {
    throw new Error("DuckDB schema migration 数量与服务端不一致");
  }
  for (const [index, migration] of MIGRATIONS.entries()) {
    const row = rows[index];
    if (
      row === undefined
      || parseVersion(row.version, "migration version") !== migration.version
      || row.description !== migration.description
    ) {
      throw new Error(`DuckDB schema migration v${migration.version} 与服务端不一致`);
    }
  }
}

/** DuckDB JSON 转换会把 BIGINT 表示为字符串；在此统一收窄为安全整数。 */
function parseVersion(value: unknown, label: string): number {
  const parsed = typeof value === "number"
    ? value
    : typeof value === "string" && /^\d+$/u.test(value)
      ? Number(value)
      : Number.NaN;
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new RangeError(`${label} 必须是非负安全整数`);
  }
  return parsed;
}
