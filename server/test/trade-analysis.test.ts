import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  calculateExtremeTrades,
  calculateHoldingPeriod,
  calculateOptionStress,
  calculateStreaks,
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

describe("holding, extremes, streaks and stress", () => {
  it("复现 Python 综合分析样本", () => {
    const ledger = normalizeTransactionBatch({
      fileName: "Individual_TST001_Transactions.csv",
      account: "TST001",
      sourceHash: "source-a",
      sourceType: "csv",
      sourceReference: "import-trade-analysis",
      rows: [
        row({ Date: "04/02/2026", Action: "Sell", Symbol: "CCC", Description: "CCC CORP", Price: "$50.00", Amount: "$49.00" }),
        row({ Date: "04/01/2026", Action: "Buy", Symbol: "CCC", Description: "CCC CORP" }),
        row({ Date: "03/03/2026", Action: "Sell", Price: "$120.00", Amount: "$119.00" }),
        row({ Date: "03/01/2026", Action: "Buy" }),
        row({ Date: "02/02/2026", Action: "Sell to Open", Symbol: "EEE 08/15/2026 60.00 C", Description: "CALL EEE", Price: "$1.00", Amount: "$99.00" }),
        row({ Date: "02/01/2026", Action: "Buy to Open", Symbol: "DDD 08/15/2026 50.00 C", Description: "CALL DDD", Quantity: "2", Price: "$2.00", Amount: "-$401.00" }),
      ],
    }).transactions;
    const fifo = replayFifo(ledger);

    const holding = calculateHoldingPeriod(fifo.realizedRecords, { kind: "all" });
    assert.equal(holding.rows[0]?.bucket, "0-7d");
    assert.equal(holding.rows[0]?.closedLots, 2);

    const extremes = calculateExtremeTrades(fifo.realizedRecords, { kind: "all" }, 1);
    assert.equal(extremes.rows[0]?.side, "best");
    assert.equal(extremes.rows[0]?.trade.underlying, "AAA");
    assert.equal(extremes.rows[1]?.side, "worst");
    assert.equal(extremes.rows[1]?.trade.underlying, "CCC");

    const streaks = calculateStreaks(fifo.realizedRecords, { kind: "all" });
    assert.equal(streaks.closeTransactions, 2);
    assert.deepEqual(
      Object.fromEntries(streaks.rows.map((streak) => [streak.kind, streak.count])),
      { win: 1, loss: 1 },
    );

    const stress = calculateOptionStress(fifo.openLots, { kind: "all" });
    assert.equal(stress.longOptionContracts, "2.000000");
    assert.equal(stress.allLongOptionsLossIfZero, "-400.00");
    assert.equal(stress.rows[0]?.underlying, "DDD");
  });

  it("拒绝非正数最佳/最差返回数量", () => {
    assert.throws(() => calculateExtremeTrades([], { kind: "all" }, 0), RangeError);
  });
});
