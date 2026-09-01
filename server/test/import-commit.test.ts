import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  buildImportCommitPlan,
  createImportPreview,
} from "../src/application/index.js";
import type { RawTransactionFields } from "../src/domain/ledger/index.js";

/** 构造会形成一次完整股票平仓的导入批次。 */
function batch(): {
  fileName: string;
  account: string;
  sourceHash: string;
  sourceType: "csv";
  sourceReference: string;
  rows: readonly RawTransactionFields[];
} {
  return {
    fileName: "Individual_TST001_Transactions.csv",
    account: "TST001",
    sourceHash: "1".repeat(64),
    sourceType: "csv",
    sourceReference: "import-commit-1",
    rows: [
      {
        Date: "01/02/2026", Action: "Sell", Symbol: "AAA", Description: "AAA CORP",
        Quantity: "1", Price: "$15.00", "Fees & Comm": "", Amount: "$15.00",
      },
      {
        Date: "01/01/2026", Action: "Buy", Symbol: "AAA", Description: "AAA CORP",
        Quantity: "1", Price: "$10.00", "Fees & Comm": "", Amount: "-$10.00",
      },
    ],
  };
}

describe("buildImportCommitPlan", () => {
  it("在写事务前筛出新增交易并完成受影响账户 FIFO 重放", () => {
    const preview = createImportPreview({
      batch: batch(),
      parserVersion: "1.0.0",
      baseLedgerVersion: 0,
      existingTransactionHashes: new Set(),
    });
    const plan = buildImportCommitPlan(preview, []);

    assert.equal(plan.changed, true);
    assert.equal(plan.insertedRows, 2);
    assert.equal(plan.skippedRows, 0);
    assert.equal(plan.fifo?.transactionsReplayed, 2);
    assert.equal(plan.fifo?.realizedRecords[0]?.pnl.toFixed(4), "5.0000");
  });

  it("重复提交返回明确无变更结果且不重复重放", () => {
    const firstPreview = createImportPreview({
      batch: batch(),
      parserVersion: "1.0.0",
      baseLedgerVersion: 0,
      existingTransactionHashes: new Set(),
    });
    const first = buildImportCommitPlan(firstPreview, []);
    const existingHashes = new Set(
      first.accountTransactions.map((transaction) => transaction.transactionHash),
    );
    const repeatedPreview = createImportPreview({
      batch: batch(),
      parserVersion: "1.0.0",
      baseLedgerVersion: 1,
      existingTransactionHashes: existingHashes,
    });
    const repeated = buildImportCommitPlan(repeatedPreview, first.accountTransactions);

    assert.equal(repeated.changed, false);
    assert.equal(repeated.insertedRows, 0);
    assert.equal(repeated.skippedRows, 2);
    assert.equal(repeated.fifo, null);
    assert.equal(repeated.accountTransactions, first.accountTransactions);
  });
});
