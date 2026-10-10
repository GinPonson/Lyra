import assert from "node:assert/strict";
import { access, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { openLyraDatabase } from "../src/infrastructure/db/database.js";

describe("DuckDB Node database", () => {
  it("在文件数据库中原子执行 v1-v13，并可重复打开", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-duckdb-node-"));
    const databasePath = join(directory, "ledger.duckdb");
    try {
      const first = await openLyraDatabase(databasePath);
      assert.deepEqual(await first.readHealth(), {
        status: "available",
        schemaVersion: 13,
        ledgerVersion: 0,
      });
      const firstHistory = await first.connection.runAndReadAll(
        "SELECT count(*)::INTEGER AS count FROM schema_migrations",
      );
      assert.equal(firstHistory.getRowObjectsJson()[0]?.count, 13);
      await assert.rejects(
        () => access(`${databasePath}.wal`),
        (error: unknown) => error instanceof Error
          && "code" in error
          && error.code === "ENOENT",
      );
      first.close();

      const reopened = await openLyraDatabase(databasePath);
      assert.deepEqual(await reopened.readHealth(), {
        status: "available",
        schemaVersion: 13,
        ledgerVersion: 0,
      });
      reopened.close();
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });

  it("拒绝用旧服务打开包含未来 migration 的数据库", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-duckdb-future-"));
    const databasePath = join(directory, "ledger.duckdb");
    try {
      const database = await openLyraDatabase(databasePath);
      await database.connection.run(
        "INSERT INTO schema_migrations (version, description) VALUES (14, 'future schema')",
      );
      database.close();

      await assert.rejects(
        () => openLyraDatabase(databasePath),
        /migration 数量与服务端不一致/u,
      );
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });
});
