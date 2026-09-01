import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ApplicationError,
  assertImportPreviewCurrent,
  createImportPreview,
  createPublicImportPreview,
} from "../src/application/index.js";
import type { RawTransactionFields } from "../src/domain/ledger/index.js";

/** 构造导入预览使用的标准 Schwab 八列。 */
function row(overrides: Partial<RawTransactionFields>): RawTransactionFields {
  return {
    Date: "01/01/2026",
    Action: "Buy",
    Symbol: "AAA",
    Description: "AAA CORP",
    Quantity: "1",
    Price: "$10.00",
    "Fees & Comm": "",
    Amount: "-$10.00",
    ...overrides,
  };
}

/** 创建两行有效输入，便于复用第一次预览生成的交易哈希。 */
function input(existingTransactionHashes: ReadonlySet<string>) {
  return {
    batch: {
      fileName: "Individual_TST001_Transactions.csv",
      account: "TST001",
      sourceHash: "f".repeat(64),
      sourceType: "csv",
      sourceReference: "import-preview-1",
      rows: [
        row({ Date: "02/02/2026", Action: "Sell", Price: "$12.00", Amount: "$12.00" }),
        row({ Date: "01/01/2026" }),
      ],
    },
    parserVersion: "1.0.0",
    baseLedgerVersion: 7,
    existingTransactionHashes,
  } as const;
}

describe("import preview application flow", () => {
  it("计算已有交易、预计新增数、日期范围并生成稳定指纹", () => {
    const initial = createImportPreview(input(new Set()));
    const existingHash = initial.transactions[0]?.transactionHash;
    assert.notEqual(existingHash, undefined);
    if (existingHash === undefined) {
      throw new Error("测试预览未生成第一笔交易哈希");
    }

    const preview = createImportPreview(input(new Set([existingHash])));
    assert.equal(preview.totalRows, 2);
    assert.equal(preview.insertableRows, 1);
    assert.equal(preview.existingRows, 1);
    assert.equal(preview.firstTransactionDate, "2026-01-01");
    assert.equal(preview.lastTransactionDate, "2026-02-02");
    assert.deepEqual(preview.byAction, { Sell: 1, Buy: 1 });
    assert.equal(preview.previewFingerprint, initial.previewFingerprint);
  });

  it("提交条件变化时明确要求重新预览", () => {
    const preview = createImportPreview(input(new Set()));
    assert.doesNotThrow(() => assertImportPreviewCurrent(preview, {
      fileHash: preview.fileHash,
      parserVersion: preview.parserVersion,
      account: preview.account,
      ledgerVersion: preview.baseLedgerVersion,
    }));
    assert.throws(
      () => assertImportPreviewCurrent(preview, {
        fileHash: preview.fileHash,
        parserVersion: "2.0.0",
        account: "TST002",
        ledgerVersion: preview.baseLedgerVersion + 1,
      }),
      (error: unknown) => (
        error instanceof ApplicationError
        && error.code === "IMPORT_PREVIEW_STALE"
        && JSON.stringify(error.details).includes("parser_version")
      ),
    );
  });

  it("公开预览只返回 App 复核统计和 Lyra 账户 ID", () => {
    const preview = createImportPreview(input(new Set()));
    const publicPreview = createPublicImportPreview(preview, " account-uuid-1 ");

    assert.deepEqual(publicPreview, {
      fileName: "Individual_TST001_Transactions.csv",
      accountId: "account-uuid-1",
      baseLedgerVersion: 7,
      totalRows: 2,
      insertableRows: 2,
      existingRows: 0,
      byAction: { Sell: 1, Buy: 1 },
      firstTransactionDate: "2026-01-01",
      lastTransactionDate: "2026-02-02",
    });
    const serialized = JSON.stringify(publicPreview);
    assert.equal(serialized.includes("TST001\""), false);
    assert.equal(serialized.includes("transactionHash"), false);
    assert.equal(serialized.includes("sourceReference"), false);
    assert.equal("transactions" in publicPreview, false);
    assert.equal("previewFingerprint" in publicPreview, false);
  });
});
