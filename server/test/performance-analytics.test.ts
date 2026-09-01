import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  calculateAllocation,
  calculateAttribution,
  calculateMonthlyPerformance,
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
    Quantity: "10",
    Price: "$10.00",
    "Fees & Comm": "$1.00",
    Amount: "-$101.00",
    ...overrides,
  };
}

describe("monthly and allocation", () => {
  it("复现 Python 月度绩效和剩余持仓历史成本配置", () => {
    const ledger = normalizeTransactionBatch({
      fileName: "Individual_TST001_Transactions.csv",
      account: "TST001",
      sourceHash: "source-a",
      sourceType: "csv",
      sourceReference: "import-performance-a",
      rows: [
        row({ Date: "03/03/2026", Action: "Sell", Quantity: "5", Price: "$12.00", Amount: "$59.00" }),
        row({ Date: "03/01/2026" }),
        row({ Date: "02/02/2026", Action: "Sell to Close", Symbol: "BBB 06/19/2026 50.00 C", Description: "CALL BBB", Quantity: "1", Price: "$3.00", Amount: "$299.00" }),
        row({ Date: "02/01/2026", Action: "Buy to Open", Symbol: "BBB 06/19/2026 50.00 C", Description: "CALL BBB", Quantity: "1", Price: "$2.00", Amount: "-$201.00" }),
      ],
    }).transactions;
    const fifo = replayFifo(ledger);

    const monthly = calculateMonthlyPerformance(fifo.realizedRecords, { kind: "all" });
    assert.equal(monthly.months, 2);
    assert.equal(monthly.totalPnl, "106.5000");
    assert.equal(monthly.rows[0]?.month, "2026-02");
    assert.equal(monthly.rows[0]?.winRatePct, "100.00");

    const allocation = calculateAllocation(fifo.openLots, { kind: "all" }, "underlying");
    assert.equal(allocation.groups, 1);
    assert.equal(allocation.rows[0]?.category, "AAA");
    assert.equal(allocation.rows[0]?.costBasis, "50.00");
    assert.equal(allocation.rows[0]?.allocationPct, "100.00");
  });

  it("拒绝反向月度日期范围", () => {
    assert.throws(
      () => calculateMonthlyPerformance([], { kind: "all" }, "2026-02-01", "2026-01-01"),
      RangeError,
    );
  });
});

describe("attribution", () => {
  it("按标的拆解已实现损益并按净贡献降序", () => {
    const ledger = normalizeTransactionBatch({
      fileName: "Individual_TST001_Transactions.csv",
      account: "TST001",
      sourceHash: "source-a",
      sourceType: "csv",
      sourceReference: "import-performance-b",
      rows: [
        row({ Date: "04/02/2026", Action: "Sell", Symbol: "CCC", Description: "CCC CORP", Quantity: "1", Price: "$50.00", Amount: "$49.00" }),
        row({ Date: "04/01/2026", Action: "Buy", Symbol: "CCC", Description: "CCC CORP", Quantity: "1", Price: "$100.00", Amount: "-$101.00" }),
        row({ Date: "03/03/2026", Action: "Sell", Symbol: "AAA", Quantity: "1", Price: "$120.00", Amount: "$119.00" }),
        row({ Date: "03/01/2026", Action: "Buy", Symbol: "AAA", Quantity: "1", Price: "$100.00", Amount: "-$101.00" }),
      ],
    }).transactions;
    const fifo = replayFifo(ledger);
    const result = calculateAttribution(
      fifo.realizedRecords,
      { kind: "all" },
      "underlying",
    );

    assert.equal(result.totalPnl, "-34.0000");
    assert.equal(result.rows[0]?.category, "AAA");
    assert.equal(result.rows[0]?.netPnl, "18.0000");
    assert.equal(result.rows[1]?.category, "CCC");
    assert.equal(result.rows[1]?.netPnl, "-52.0000");
  });
});
