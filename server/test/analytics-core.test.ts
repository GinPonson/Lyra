import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  aggregatePositions,
  analyzeExpiringPositions,
  calculateAccountSummary,
} from "../src/domain/analytics/index.js";
import { replayFifo } from "../src/domain/fifo/index.js";
import { normalizeTransactionBatch } from "../src/domain/ledger/index.js";
import type {
  ParsedTransaction,
  RawTransactionFields,
} from "../src/domain/ledger/index.js";

/** 构造有效 Schwab 八列交易。 */
function row(overrides: Partial<RawTransactionFields>): RawTransactionFields {
  return {
    Date: "01/01/2026",
    Action: "Buy",
    Symbol: "AAA",
    Description: "AAA CORP",
    Quantity: "10",
    Price: "$10.00",
    "Fees & Comm": "",
    Amount: "-$100.00",
    ...overrides,
  };
}

/** 把文件顺序的原始行转换为标准交易。 */
function parseRows(rows: readonly RawTransactionFields[]): readonly ParsedTransaction[] {
  return normalizeTransactionBatch({
    fileName: "Individual_TST001_Transactions.csv",
    account: "TST001",
    sourceHash: "source-a",
    sourceType: "csv",
    sourceReference: "import-analytics-core",
    rows,
  }).transactions;
}

describe("aggregatePositions", () => {
  it("按历史成本聚合剩余批次并保留最早开仓日", () => {
    const ledger = parseRows([
      row({ Date: "01/03/2026", Action: "Sell", Quantity: "5", Price: "$30.00", Amount: "$150.00" }),
      row({ Date: "01/02/2026", Price: "$20.00", Amount: "-$200.00" }),
      row({ Date: "01/01/2026", Price: "$10.00", Amount: "-$100.00" }),
    ]);
    const fifo = replayFifo(ledger);
    const positions = aggregatePositions(fifo.openLots, { kind: "account", account: "TST001" });

    assert.equal(positions.length, 1);
    assert.equal(positions[0]?.quantity.toString(), "15");
    assert.equal(positions[0]?.cost.toFixed(2), "250.00");
    assert.equal(positions[0]?.firstOpenDate, "2026-01-01");
  });
});

describe("analyzeExpiringPositions", () => {
  it("相对基准日区分已过期和窗口内即将到期仓位", () => {
    const ledger = parseRows([
      row({
        Date: "07/02/2026",
        Action: "Buy to Open",
        Symbol: "OLD 07/31/2026 100.00 C",
        Description: "CALL OLD",
        Quantity: "1",
        Price: "$1.00",
        "Fees & Comm": "$0.66",
        Amount: "-$100.66",
      }),
      row({
        Date: "07/01/2026",
        Action: "Buy to Open",
        Symbol: "NEW 08/15/2026 100.00 C",
        Description: "CALL NEW",
        Quantity: "2",
        Price: "$2.00",
        "Fees & Comm": "$1.31",
        Amount: "-$401.31",
      }),
    ]);
    const fifo = replayFifo(ledger);
    const positions = aggregatePositions(fifo.openLots, { kind: "all" });
    const result = analyzeExpiringPositions(positions, "2026-08-01", 30);

    assert.equal(result.expired, 1);
    assert.equal(result.upcoming, 1);
    assert.equal(result.expiresToday, 0);
    assert.equal(result.rows[0]?.daysRemaining, -1);
    assert.equal(result.rows[1]?.premiumDollars.toFixed(2), "400.00");
  });

  it("拒绝负窗口和不存在的基准日期", () => {
    assert.throws(() => analyzeExpiringPositions([], "2026-08-01", -1), RangeError);
    assert.throws(() => analyzeExpiringPositions([], "2026-02-30", 30), RangeError);
  });
});

describe("calculateAccountSummary", () => {
  it("按显式账户范围汇总流水、费用、已实现损益和日期区间", () => {
    const ledger = parseRows([
      row({ Date: "03/03/2026", Action: "Sell", Quantity: "5", Price: "$12.00", "Fees & Comm": "$1.00", Amount: "$59.00" }),
      row({ Date: "03/01/2026", Action: "Buy", Quantity: "10", Price: "$10.00", "Fees & Comm": "$1.00", Amount: "-$101.00" }),
      row({ Date: "02/15/2026", Action: "Qualified Dividend", Quantity: "", Price: "", "Fees & Comm": "", Amount: "$5.00" }),
      row({ Date: "01/01/2026", Action: "MoneyLink Transfer", Symbol: "", Description: "Deposit", Quantity: "", Price: "", "Fees & Comm": "", Amount: "$1000.00" }),
    ]);
    const fifo = replayFifo(ledger);
    const summary = calculateAccountSummary(
      ledger,
      fifo,
      { kind: "account", account: "TST001" },
    );

    // 股票已实现：(12 - 10) × 5 - 开仓费 0.50 - 平仓费 1.00 = 8.50。
    assert.equal(summary.realizedPnlStock, "8.5000");
    assert.equal(summary.netDeposits, "1000.0000");
    assert.equal(summary.dividends, "5.0000");
    assert.equal(summary.totalFees, "2.000000");
    assert.equal(summary.cashBalance, "963.0000");
    assert.equal(summary.openPositions, 1);
    assert.equal(summary.transactionCount, 4);
    assert.equal(summary.dateRange, "2026-01-01 ~ 2026-03-03");
  });
});
