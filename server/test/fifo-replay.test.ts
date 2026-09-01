import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { Decimal } from "decimal.js";

import {
  FifoError,
  replayFifo,
} from "../src/domain/fifo/index.js";
import { normalizeTransactionBatch } from "../src/domain/ledger/index.js";
import type {
  ParsedTransaction,
  RawTransactionFields,
} from "../src/domain/ledger/index.js";

/** 把与 Python fixture 相同顺序的原始行转换为 FIFO 输入。 */
function transactions(
  rows: readonly RawTransactionFields[],
  sourceHash = "source-a",
): readonly ParsedTransaction[] {
  return normalizeTransactionBatch({
    fileName: "Individual_TST001_Transactions.csv",
    account: "TST001",
    sourceHash,
    sourceType: "csv",
    sourceReference: sourceHash,
    rows,
  }).transactions;
}

/** 构造具有完整八列的测试交易。 */
function row(overrides: Partial<RawTransactionFields>): RawTransactionFields {
  return {
    Date: "01/05/2026",
    Action: "Buy",
    Symbol: "INTC",
    Description: "INTEL CORP",
    Quantity: "10",
    Price: "$40.00",
    "Fees & Comm": "",
    Amount: "-$400.00",
    ...overrides,
  };
}

describe("replayFifo stock", () => {
  it("跨多个批次先进先出并保留剩余批次", () => {
    const result = replayFifo(transactions([
      row({ Date: "01/10/2026", Action: "Sell", Quantity: "15", Price: "$60.00", Amount: "$900.00" }),
      row({ Date: "01/06/2026", Price: "$50.00", Amount: "-$500.00" }),
      row({ Date: "01/05/2026", Price: "$40.00", Amount: "-$400.00" }),
    ]));

    assert.equal(result.realizedRecords.length, 2);
    assert.equal(result.realizedRecords[0]?.quantity.toString(), "10");
    assert.equal(result.realizedRecords[0]?.pnl.toFixed(4), "200.0000");
    assert.equal(result.realizedRecords[1]?.quantity.toString(), "5");
    assert.equal(result.realizedRecords[1]?.pnl.toFixed(4), "50.0000");
    assert.equal(result.openLots[0]?.remainingQuantity.toString(), "5");
    assert.equal(result.openLots[0]?.openPrice.toFixed(2), "50.00");
  });

  it("拒绝卖出数量超过未平数量", () => {
    assert.throws(
      () => replayFifo(transactions([
        row({ Date: "01/06/2026", Action: "Sell", Quantity: "20", Price: "$60.00", Amount: "$1200.00" }),
        row({ Date: "01/05/2026" }),
      ])),
      (error: unknown) => error instanceof FifoError && error.message.includes("超过未平数量"),
    );
  });

  it("同日按文件物理顺序降序重放", () => {
    const result = replayFifo(transactions([
      row({ Action: "Sell", Price: "$60.00", Amount: "$600.00" }),
      row({ Action: "Buy", Price: "$40.00", Amount: "-$400.00" }),
    ]));
    assert.equal(result.realizedRecords[0]?.pnl.toFixed(4), "200.0000");
  });
});

describe("replayFifo option", () => {
  it("多头期权到期损失权利金和开仓费", () => {
    const result = replayFifo(transactions([
      row({
        Date: "05/18/2026 as of 05/15/2026",
        Action: "Expired",
        Symbol: "NVDA 05/15/2026 160.00 P",
        Description: "PUT NVIDIA",
        Quantity: "1",
        Price: "",
        Amount: "",
      }),
      row({
        Date: "04/01/2026",
        Action: "Buy to Open",
        Symbol: "NVDA 05/15/2026 160.00 P",
        Description: "PUT NVIDIA",
        Quantity: "1",
        Price: "$2.00",
        "Fees & Comm": "$0.66",
        Amount: "-$200.66",
      }),
    ]));
    assert.equal(result.totalRealizedPnl, "-200.6600");
    assert.equal(result.openLots.length, 0);
  });

  it("空头期权到期保留权利金并扣除费用", () => {
    const result = replayFifo(transactions([
      row({
        Date: "05/18/2026 as of 05/15/2026",
        Action: "Expired",
        Symbol: "INTC 05/15/2026 55.00 P",
        Description: "PUT INTEL",
        Quantity: "1",
        Price: "",
        Amount: "",
      }),
      row({
        Date: "04/22/2026",
        Action: "Sell to Open",
        Symbol: "INTC 05/15/2026 55.00 P",
        Description: "PUT INTEL",
        Quantity: "1",
        Price: "$1.18",
        "Fees & Comm": "$0.66",
        Amount: "$117.34",
      }),
    ]));
    assert.equal(result.totalRealizedPnl, "117.3400");
    assert.equal(result.realizedRecords[0]?.direction, "short");
  });

  it("Assigned 按零平空头期权，股票交割独立走股票 FIFO", () => {
    const result = replayFifo(transactions([
      row({
        Date: "04/20/2026 as of 04/17/2026",
        Action: "Sell",
        Quantity: "100",
        Price: "$50.00",
        "Fees & Comm": "$0.12",
        Amount: "$4999.88",
      }),
      row({
        Date: "04/20/2026 as of 04/17/2026",
        Action: "Assigned",
        Symbol: "INTC 04/17/2026 50.00 C",
        Description: "CALL INTEL",
        Quantity: "1",
        Price: "",
        Amount: "",
      }),
      row({
        Date: "03/23/2026",
        Action: "Sell to Open",
        Symbol: "INTC 04/17/2026 50.00 C",
        Description: "CALL INTEL",
        Quantity: "1",
        Price: "$1.16",
        "Fees & Comm": "$0.66",
        Amount: "$115.34",
      }),
      row({ Date: "01/05/2026", Quantity: "100", Amount: "-$4000.00" }),
    ]));

    const option = result.realizedRecords.find((record) => record.assetType === "option");
    const stock = result.realizedRecords.find((record) => record.assetType === "stock");
    assert.equal(option?.pnl.toFixed(4), "115.3400");
    assert.equal(stock?.pnl.toFixed(4), "999.8800");
  });

  it("跨期权批次分摊开仓和平仓费用且无尾差", () => {
    const result = replayFifo(transactions([
      row({
        Date: "07/15/2026",
        Action: "Sell to Close",
        Symbol: "GOOG 03/19/2027 440.00 C",
        Description: "CALL ALPHABET",
        Quantity: "2",
        Price: "$23.35",
        "Fees & Comm": "$0.71",
        Amount: "$4669.29",
      }),
      row({
        Date: "07/10/2026",
        Action: "Buy to Open",
        Symbol: "GOOG 03/19/2027 440.00 C",
        Description: "CALL ALPHABET",
        Quantity: "1",
        Price: "$20.00",
        "Fees & Comm": "$0.66",
        Amount: "-$2000.66",
      }),
      row({
        Date: "07/01/2026",
        Action: "Buy to Open",
        Symbol: "GOOG 03/19/2027 440.00 C",
        Description: "CALL ALPHABET",
        Quantity: "1",
        Price: "$19.00",
        "Fees & Comm": "$0.66",
        Amount: "-$1900.66",
      }),
    ]));

    const closeFees = result.realizedRecords.reduce(
      (total, record) => total.plus(record.closeFees),
      new Decimal(0),
    );
    assert.equal(result.realizedRecords[0]?.openFees.toFixed(2), "0.66");
    assert.equal(closeFees.toFixed(2), "0.71");
    assert.equal(result.totalRealizedPnl, "767.9700");
  });

  it("没有现有持仓时拒绝 Expired", () => {
    assert.throws(
      () => replayFifo(transactions([
        row({
          Date: "05/18/2026 as of 05/15/2026",
          Action: "Expired",
          Symbol: "NVDA 05/15/2026 160.00 P",
          Description: "PUT NVIDIA",
          Quantity: "1",
          Price: "",
          Amount: "",
        }),
      ])),
      (error: unknown) => error instanceof FifoError && error.message.includes("无法确定持仓方向"),
    );
  });
});

describe("replayFifo ordering", () => {
  it("拒绝同账户同日同合约来自多个未对齐快照", () => {
    const buy = transactions([row({ Action: "Buy" })], "source-a");
    const sell = transactions([
      row({ Action: "Sell", Price: "$60.00", Amount: "$600.00" }),
    ], "source-b");

    assert.throws(
      () => replayFifo([...buy, ...sell]),
      (error: unknown) => error instanceof FifoError && error.message.includes("未对齐的 CSV 快照"),
    );
  });

  it("对同一输入重复重放得到相同汇总且不修改输入", () => {
    const input = transactions([
      row({ Date: "01/10/2026", Action: "Sell", Price: "$60.00", Amount: "$600.00" }),
      row({ Date: "01/05/2026" }),
    ]);
    const first = replayFifo(input);
    const second = replayFifo(input);
    assert.equal(first.totalRealizedPnl, "200.0000");
    assert.equal(second.totalRealizedPnl, first.totalRealizedPnl);
    assert.equal(input[1]?.quantity?.toString(), "10");
  });
});
