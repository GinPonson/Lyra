import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createPublicImportJob } from "../src/application/index.js";
import type { PersistedImportJobRecord } from "../src/application/index.js";

const JOB: PersistedImportJobRecord = {
  id: "import-1",
  fileHash: "a".repeat(64),
  originalFileName: "Individual_Transactions.xlsx",
  fileType: "xlsx",
  fileSizeBytes: 84_210,
  parserVersion: "schwab-ts-1",
  status: "failed",
  targetAccountId: "account-1",
  previewTokenHash: "SENSITIVE PREVIEW TOKEN HASH",
  previewLedgerVersion: 22,
  totalRows: 140,
  insertedRows: 0,
  skippedRows: 0,
  errorCode: "LEDGER_VERSION_CONFLICT",
  errorSummary: "SENSITIVE DATABASE ERROR DETAIL",
  committedLedgerVersion: null,
  createdAt: "2026-08-31T17:00:00.000Z",
  updatedAt: "2026-08-31T17:01:00.000Z",
};

describe("public import job", () => {
  it("公开可恢复状态和稳定错误码且隔离提交与内部错误字段", () => {
    const result = createPublicImportJob(JOB);

    assert.deepEqual(result, {
      id: "import-1",
      fileName: "Individual_Transactions.xlsx",
      fileType: "xlsx",
      fileSizeBytes: 84_210,
      status: "failed",
      targetAccountId: "account-1",
      baseLedgerVersion: 22,
      totalRows: 140,
      insertedRows: 0,
      skippedRows: 0,
      errorCode: "LEDGER_VERSION_CONFLICT",
      committedLedgerVersion: null,
      createdAt: "2026-08-31T17:00:00.000Z",
      updatedAt: "2026-08-31T17:01:00.000Z",
    });
    const serialized = JSON.stringify(result);
    for (const forbidden of [
      "fileHash",
      "parserVersion",
      "previewTokenHash",
      "SENSITIVE PREVIEW TOKEN HASH",
      "errorSummary",
      "SENSITIVE DATABASE ERROR DETAIL",
    ]) {
      assert.equal(serialized.includes(forbidden), false);
    }
    assert.throws(
      () => createPublicImportJob({ ...JOB, errorCode: "raw SQL error" }),
      RangeError,
    );
  });
});
