import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  IngestError,
  normalizeTransactionBatch,
} from "../src/domain/ledger/index.js";
import type { RawTransactionFields } from "../src/domain/ledger/index.js";

/** 构造有效股票买入行，调用方只覆盖当前场景相关字段。 */
function stockRow(overrides: Partial<RawTransactionFields> = {}): RawTransactionFields {
  return {
    Date: "07/28/2026",
    Action: "Buy",
    Symbol: "INTC",
    Description: "INTEL CORP",
    Quantity: "10",
    Price: "$85.23",
    "Fees & Comm": "",
    Amount: "-$852.30",
    ...overrides,
  };
}

describe("normalizeTransactionBatch", () => {
  it("在全部行有效时返回动作统计和连续物理顺序", () => {
    const preview = normalizeTransactionBatch({
      fileName: "Individual_TST001_Transactions.csv",
      account: "TST001",
      sourceHash: "digest",
      sourceType: "csv",
      sourceReference: "import-batch-1",
      rows: [
        stockRow(),
        stockRow({
          Action: "Qualified Dividend",
          Quantity: "",
          Price: "",
          Amount: "$5.00",
        }),
      ],
    });

    assert.equal(preview.totalRows, 2);
    assert.deepEqual(preview.byAction, { Buy: 1, "Qualified Dividend": 1 });
    assert.deepEqual(preview.transactions.map((transaction) => transaction.sequence), [1, 2]);
  });

  it("为业务字段完全相同的真实交易分配递增出现次数", () => {
    const duplicateCashRow = stockRow({
      Date: "10/28/2025 as of 10/27/2025",
      Action: "MoneyLink Transfer",
      Symbol: "",
      Description: "Tfr WISE",
      Quantity: "",
      Price: "",
      Amount: "$5.00",
    });
    const preview = normalizeTransactionBatch({
      fileName: "Individual_TST001_Transactions.csv",
      account: "TST001",
      sourceHash: "digest",
      sourceType: "csv",
      sourceReference: "import-batch-2",
      rows: [duplicateCashRow, duplicateCashRow],
    });

    const [first, second] = preview.transactions;
    assert.equal(first?.occurrence, 1);
    assert.equal(second?.occurrence, 2);
    assert.equal(first?.businessHash, second?.businessHash);
    assert.notEqual(first?.transactionHash, second?.transactionHash);

    const gmailPreview = normalizeTransactionBatch({
      fileName: "gmail:message-2",
      account: "TST001",
      sourceHash: "different-content-digest",
      sourceType: "gmail",
      sourceReference: "message-2",
      rows: [duplicateCashRow, duplicateCashRow],
    });
    assert.deepEqual(
      gmailPreview.transactions.map((transaction) => transaction.transactionHash),
      preview.transactions.map((transaction) => transaction.transactionHash),
    );
  });

  it("任一行无效时抛出带文件行号的错误而不返回部分预览", () => {
    assert.throws(
      () => normalizeTransactionBatch({
        fileName: "Individual_TST001_Transactions.csv",
        account: "TST001",
        sourceHash: "digest",
        sourceType: "csv",
        sourceReference: "import-batch-invalid",
        rows: [stockRow(), stockRow({ Action: "Unknown Action" })],
      }),
      (error: unknown) => (
        error instanceof IngestError
        && error.message.includes("Individual_TST001_Transactions.csv 第2行")
      ),
    );
  });
});
