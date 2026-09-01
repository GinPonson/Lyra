import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import {
  buildImportCommitPlan,
  createImportPreview,
  type ImportCommitPlan,
} from "../src/application/index.js";
import type { RawTransactionFields } from "../src/domain/ledger/index.js";
import { openLyraDatabase, type LyraDatabase } from "../src/infrastructure/db/database.js";
import { persistImportCommit } from "../src/infrastructure/db/import-commit-repository.js";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const IMPORT_ID = "22222222-2222-4222-8222-222222222222";
const SECOND_IMPORT_ID = "33333333-3333-4333-8333-333333333333";
const FINGERPRINT = "a".repeat(64);
const PARSER_VERSION = "schwab-csv-v1";

/** 构造一开一平、可产生 $5 已实现损益的标准批次。 */
function transactionRows(): readonly RawTransactionFields[] {
  return [
    {
      Date: "01/02/2026", Action: "Sell", Symbol: "AAA", Description: "AAA CORP",
      Quantity: "1", Price: "$15.00", "Fees & Comm": "", Amount: "$15.00",
    },
    {
      Date: "01/01/2026", Action: "Buy", Symbol: "AAA", Description: "AAA CORP",
      Quantity: "1", Price: "$10.00", "Fees & Comm": "", Amount: "-$10.00",
    },
  ];
}

/** 生成绑定指定导入 ID 与账本版本的预览。 */
function createPlan(
  importId: string,
  baseLedgerVersion: number,
  existing: ImportCommitPlan["accountTransactions"] = [],
): ImportCommitPlan {
  const preview = createImportPreview({
    batch: {
      fileName: `Individual_TST001_Transactions_${importId}.csv`,
      account: "TST001",
      sourceHash: "1".repeat(64),
      sourceType: "csv",
      sourceReference: importId,
      rows: transactionRows(),
    },
    parserVersion: PARSER_VERSION,
    baseLedgerVersion,
    existingTransactionHashes: new Set(
      existing.map((transaction) => transaction.transactionHash),
    ),
  });
  return buildImportCommitPlan(preview, existing);
}

/** 插入提交事务所需的账户与 preview_ready 作业。 */
async function seedPreviewJob(
  database: LyraDatabase,
  importId: string,
  baseLedgerVersion: number,
): Promise<void> {
  await database.connection.run(`
    INSERT INTO accounts (
      id, display_name, legacy_account_key, masked_suffix, created_at, updated_at
    )
    SELECT
      CAST($account_id AS UUID), 'Primary', 'TST001', '001',
      CAST('2026-08-31T09:00:00Z' AS TIMESTAMPTZ),
      CAST('2026-08-31T09:00:00Z' AS TIMESTAMPTZ)
    WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE id = CAST($account_id AS UUID))
  `, { account_id: ACCOUNT_ID });
  await database.connection.run(`
    INSERT INTO import_files (
      id, file_hash, original_file_name, file_type, file_size_bytes, parser_version,
      status, target_account_id, preview_token_hash, preview_ledger_version,
      preview_fingerprint, preview_expires_at, total_rows, created_at, updated_at
    ) VALUES (
      CAST($import_id AS UUID), $file_hash, $file_name, 'csv', 512, $parser_version,
      'preview_ready', CAST($account_id AS UUID), 'token-hash',
      CASE WHEN $base_ledger_version = 0 THEN NULL ELSE $base_ledger_version END,
      $preview_fingerprint, CAST('2026-08-31T10:00:00Z' AS TIMESTAMPTZ), 2,
      CAST('2026-08-31T09:00:00Z' AS TIMESTAMPTZ),
      CAST('2026-08-31T09:00:00Z' AS TIMESTAMPTZ)
    )
  `, {
    import_id: importId,
    file_hash: "1".repeat(64),
    file_name: `Individual_TST001_Transactions_${importId}.csv`,
    parser_version: PARSER_VERSION,
    account_id: ACCOUNT_ID,
    base_ledger_version: baseLedgerVersion,
    preview_fingerprint: FINGERPRINT,
  });
}

describe("DuckDB import commit repository", () => {
  it("原子写入交易与 FIFO，并让无新增的后续导入不生成新账本版本", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-import-commit-"));
    const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
    try {
      await seedPreviewJob(database, IMPORT_ID, 0);
      const firstPlan = createPlan(IMPORT_ID, 0);
      const first = await persistImportCommit(database, {
        plan: firstPlan,
        importId: IMPORT_ID,
        accountId: ACCOUNT_ID,
        parserVersion: PARSER_VERSION,
        previewFingerprint: FINGERPRINT,
        expectedLedgerVersion: 0,
        committedAt: new Date("2026-08-31T09:05:00Z"),
      });
      assert.deepEqual(first, {
        changed: true,
        ledgerVersion: 1,
        insertedRows: 2,
        skippedRows: 0,
      });

      const counts = await database.connection.runAndReadAll(`
        SELECT
          (SELECT count(*)::INTEGER FROM transactions) AS transactions,
          (SELECT count(*)::INTEGER FROM lots) AS lots,
          (SELECT count(*)::INTEGER FROM realized) AS realized,
          (SELECT count(*)::INTEGER FROM ledger_versions) AS ledger_versions
      `);
      assert.deepEqual(counts.getRowObjectsJson()[0], {
        transactions: 2,
        lots: 0,
        realized: 1,
        ledger_versions: 1,
      });
      const pnl = await database.connection.runAndReadAll(
        "SELECT pnl::VARCHAR AS pnl FROM realized",
      );
      assert.equal(pnl.getRowObjectsJson()[0]?.pnl, "5.0000");

      await seedPreviewJob(database, SECOND_IMPORT_ID, 1);
      const repeatedPlan = createPlan(SECOND_IMPORT_ID, 1, firstPlan.accountTransactions);
      const repeated = await persistImportCommit(database, {
        plan: repeatedPlan,
        importId: SECOND_IMPORT_ID,
        accountId: ACCOUNT_ID,
        parserVersion: PARSER_VERSION,
        previewFingerprint: FINGERPRINT,
        expectedLedgerVersion: 1,
        committedAt: new Date("2026-08-31T09:10:00Z"),
      });
      assert.deepEqual(repeated, {
        changed: false,
        ledgerVersion: 1,
        insertedRows: 0,
        skippedRows: 2,
      });
      assert.equal((await database.connection.runAndReadAll(
        "SELECT count(*)::INTEGER AS count FROM ledger_versions",
      )).getRowObjectsJson()[0]?.count, 1);
    } finally {
      database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });

  it("任一事实写入失败时回滚账本、交易和作业状态", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-import-rollback-"));
    const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
    try {
      await seedPreviewJob(database, IMPORT_ID, 0);
      const validPlan = createPlan(IMPORT_ID, 0);
      const invalidPlan: ImportCommitPlan = {
        ...validPlan,
        insertedTransactions: validPlan.insertedTransactions.map((transaction, index) => (
          index === 1 ? { ...transaction, sourceHash: "invalid" } : transaction
        )),
      };

      await assert.rejects(() => persistImportCommit(database, {
        plan: invalidPlan,
        importId: IMPORT_ID,
        accountId: ACCOUNT_ID,
        parserVersion: PARSER_VERSION,
        previewFingerprint: FINGERPRINT,
        expectedLedgerVersion: 0,
        committedAt: new Date("2026-08-31T09:05:00Z"),
      }));

      const state = await database.connection.runAndReadAll(`
        SELECT
          (SELECT status FROM import_files WHERE id = CAST($import_id AS UUID)) AS status,
          (SELECT count(*)::INTEGER FROM ledger_versions) AS ledger_versions,
          (SELECT count(*)::INTEGER FROM transactions) AS transactions,
          (SELECT count(*)::INTEGER FROM realized) AS realized
      `, { import_id: IMPORT_ID });
      assert.deepEqual(state.getRowObjectsJson()[0], {
        status: "preview_ready",
        ledger_versions: 0,
        transactions: 0,
        realized: 0,
      });
    } finally {
      database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
});
