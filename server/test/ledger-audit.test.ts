import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { auditLedger } from "../src/domain/analytics/index.js";
import { replayFifo } from "../src/domain/fifo/index.js";
import { normalizeTransactionBatch } from "../src/domain/ledger/index.js";
import type { RawTransactionFields } from "../src/domain/ledger/index.js";

/** 构造股票买入行。 */
function buy(symbol: string, date: string, price: string): RawTransactionFields {
  return {
    Date: date,
    Action: "Buy",
    Symbol: symbol,
    Description: `${symbol} CORP`,
    Quantity: "1",
    Price: `$${price}`,
    "Fees & Comm": "",
    Amount: `-$${price}`,
  };
}

describe("auditLedger", () => {
  it("通过与当前交易一致的 FIFO 产物", () => {
    const ledger = normalizeTransactionBatch({
      fileName: "Individual_TST001_Transactions_A.csv",
      account: "TST001",
      sourceHash: "source-a",
      sourceType: "csv",
      sourceReference: "import-a",
      rows: [buy("AAA", "01/01/2026", "10.00")],
    }).transactions;
    const result = auditLedger(ledger, replayFifo(ledger), { kind: "all" });

    assert.equal(result.ok, true);
    assert.equal(result.failed, 0);
    assert.equal(result.transactionCount, 1);
  });

  it("识别导入新持仓后尚未重建的交易", () => {
    const original = normalizeTransactionBatch({
      fileName: "Individual_TST001_Transactions_A.csv",
      account: "TST001",
      sourceHash: "source-a",
      sourceType: "csv",
      sourceReference: "import-a",
      rows: [buy("AAA", "01/01/2026", "10.00")],
    }).transactions;
    const later = normalizeTransactionBatch({
      fileName: "Individual_TST001_Transactions_B.csv",
      account: "TST001",
      sourceHash: "source-b",
      sourceType: "csv",
      sourceReference: "import-b",
      rows: [buy("BBB", "01/02/2026", "20.00")],
    }).transactions;
    const result = auditLedger([...original, ...later], replayFifo(original), { kind: "all" });
    const stale = result.rows.find((row) => row.check === "unreflected_fifo_transactions");

    assert.equal(result.ok, false);
    assert.equal(stale?.status, "fail");
    assert.equal(stale?.count, 1);
  });
});
