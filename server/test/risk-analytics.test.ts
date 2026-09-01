import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  calculateDrawdown,
  calculateOptionRisk,
} from "../src/domain/analytics/index.js";
import { replayFifo } from "../src/domain/fifo/index.js";
import { normalizeTransactionBatch } from "../src/domain/ledger/index.js";
import type { RawTransactionFields } from "../src/domain/ledger/index.js";

/** 构造有效 Schwab 八列交易。 */
function row(overrides: Partial<RawTransactionFields>): RawTransactionFields {
  return {
    Date: "03/01/2026",
    Action: "Buy",
    Symbol: "AAA",
    Description: "AAA CORP",
    Quantity: "1",
    Price: "$100.00",
    "Fees & Comm": "$1.00",
    Amount: "-$101.00",
    ...overrides,
  };
}

describe("drawdown", () => {
  it("复现 Python 累计已实现损益的峰谷回撤", () => {
    const ledger = normalizeTransactionBatch({
      fileName: "Individual_TST001_Transactions.csv",
      account: "TST001",
      sourceHash: "source-a",
      sourceType: "csv",
      sourceReference: "import-risk-a",
      rows: [
        row({ Date: "04/02/2026", Action: "Sell", Symbol: "CCC", Description: "CCC CORP", Price: "$50.00", Amount: "$49.00" }),
        row({ Date: "04/01/2026", Action: "Buy", Symbol: "CCC", Description: "CCC CORP" }),
        row({ Date: "03/03/2026", Action: "Sell", Price: "$120.00", Amount: "$119.00" }),
        row({ Date: "03/01/2026", Action: "Buy" }),
      ],
    }).transactions;
    const fifo = replayFifo(ledger);
    const result = calculateDrawdown(fifo.realizedRecords, { kind: "all" });

    assert.equal(result.endingCumulativePnl, "-34.0000");
    assert.equal(result.maxDrawdown, "52.0000");
    assert.equal(result.peakDate, "2026-03-03");
    assert.equal(result.troughDate, "2026-04-02");
    assert.equal(result.recoveryDate, null);
  });
});

describe("option risk", () => {
  it("分别统计长期权成本、短期权收入和标的集中度", () => {
    const ledger = normalizeTransactionBatch({
      fileName: "Individual_TST001_Transactions.csv",
      account: "TST001",
      sourceHash: "source-a",
      sourceType: "csv",
      sourceReference: "import-risk-b",
      rows: [
        row({ Date: "02/02/2026", Action: "Sell to Open", Symbol: "EEE 08/15/2026 60.00 C", Description: "CALL EEE", Price: "$1.00", Amount: "$99.00" }),
        row({ Date: "02/01/2026", Action: "Buy to Open", Symbol: "DDD 08/15/2026 50.00 C", Description: "CALL DDD", Quantity: "2", Price: "$2.00", Amount: "-$401.00" }),
      ],
    }).transactions;
    const fifo = replayFifo(ledger);
    const result = calculateOptionRisk(fifo.openLots, { kind: "all" }, "2026-08-01");

    assert.equal(result.optionPositions, 2);
    assert.equal(result.totalLongPremiumPaid, "400.00");
    assert.equal(result.totalShortPremiumReceived, "100.00");
    assert.equal(result.rows[0]?.bucket, "0-30d");
    assert.equal(result.rows[0]?.longPremiumPaid, "400.00");
    assert.equal(result.rows[0]?.shortPremiumReceived, "100.00");
    assert.equal(result.topThreeLongUnderlyings[0]?.underlying, "DDD");
  });
});
