import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  queryCashflow,
  queryRealized,
  queryTrades,
} from "../src/domain/analytics/index.js";
import { replayFifo } from "../src/domain/fifo/index.js";
import { normalizeTransactionBatch } from "../src/domain/ledger/index.js";
import type {
  LedgerSourceType,
  ParsedTransaction,
  RawTransactionFields,
} from "../src/domain/ledger/index.js";

/** 构造完整 Schwab 八列，调用方只覆盖当前样本需要的字段。 */
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

/** 为指定账户生成标准交易，模拟独立 Schwab 导出文件。 */
function accountRows(
  account: string,
  sourceHash: string,
  rows: readonly RawTransactionFields[],
  sourceType: LedgerSourceType = "csv",
): readonly ParsedTransaction[] {
  return normalizeTransactionBatch({
    fileName: `Individual_${account}_Transactions.csv`,
    account,
    sourceHash,
    sourceType,
    sourceReference: sourceHash,
    rows,
  }).transactions;
}

/** 两个账户的共享查询样本。 */
function fixture(): {
  transactions: readonly ParsedTransaction[];
  fifo: ReturnType<typeof replayFifo>;
} {
  const transactions = [
    ...accountRows("TST001", "source-1", [
      row({
        Date: "01/04/2026", Action: "Credit Interest", Symbol: "", Description: "Interest",
        Quantity: "", Price: "", Amount: "$0.50",
      }),
      row({ Date: "01/03/2026", Action: "Sell", Price: "$15.00", Amount: "$15.00" }),
      row({
        Date: "01/02/2026", Action: "Qualified Dividend", Symbol: "", Description: "Dividend",
        Quantity: "", Price: "", Amount: "$1.00",
      }),
      row({ Date: "01/01/2026" }),
    ]),
    ...accountRows("TST002", "source-2", [
      row({ Date: "01/05/2026", Action: "Sell", Price: "$25.00", Amount: "$25.00" }),
      row({ Date: "01/01/2026", Price: "$20.00", Amount: "-$20.00" }),
    ]),
  ];
  return { transactions, fifo: replayFifo(transactions) };
}

describe("basic ledger queries", () => {
  it("按账户、标的和日期查询交易并保持倒序", () => {
    const { transactions } = fixture();
    const result = queryTrades(
      transactions,
      { kind: "account", account: "TST001" },
      { symbol: "aaa", fromDate: "2026-01-01", toDate: "2026-01-03", limit: 10 },
    );

    assert.deepEqual(result.rows.map((trade) => trade.action), ["Sell", "Buy"]);
    assert.equal(result.count, 2);
    const gmailTransactions = accountRows("TST001", "source-gmail", [
      row({ Date: "01/06/2026", Symbol: "BBB", Description: "BBB CORP" }),
    ], "gmail");
    const gmailResult = queryTrades(
      [...transactions, ...gmailTransactions],
      { kind: "account", account: "TST001" },
      { sourceType: "gmail" },
    );
    assert.equal(gmailResult.count, 1);
    assert.equal(gmailResult.rows[0]?.sourceReference, "source-gmail");
  });

  it("按账户返回已实现明细及四位小数合计", () => {
    const { fifo } = fixture();
    const result = queryRealized(
      fifo.realizedRecords,
      { kind: "account", account: "TST001" },
      { symbol: "AAA" },
    );

    assert.equal(result.count, 1);
    assert.equal(result.totalPnl, "5.0000");
    assert.equal(result.rows[0]?.account, "TST001");
  });

  it("只汇总范围内的现金交易并按动作分类", () => {
    const { transactions } = fixture();
    const result = queryCashflow(
      transactions,
      { kind: "account", account: "TST001" },
    );

    assert.deepEqual(result.rows.map((trade) => trade.action), [
      "Credit Interest",
      "Qualified Dividend",
    ]);
    assert.equal(result.total, "1.5000");
    assert.deepEqual(result.byAction, {
      "Credit Interest": "0.5000",
      "Qualified Dividend": "1.0000",
    });

    const gmailCash = accountRows("TST001", "gmail-cash", [row({
      Date: "01/06/2026", Action: "Credit Interest", Symbol: "", Description: "Interest",
      Quantity: "", Price: "", Amount: "$2.00",
    })], "gmail");
    const gmailResult = queryCashflow(
      [...transactions, ...gmailCash],
      { kind: "account", account: "TST001" },
      { sourceType: "gmail" },
    );
    assert.equal(gmailResult.count, 1);
    assert.equal(gmailResult.total, "2.0000");
  });

  it("使用绑定账户范围和筛选条件的稳定游标连续分页", () => {
    const { transactions } = fixture();
    const expected = queryTrades(transactions, { kind: "all" }, { limit: 200 });
    const pagedHashes: string[] = [];
    let cursor: string | undefined;

    do {
      const page = queryTrades(
        transactions,
        { kind: "all" },
        cursor === undefined ? { limit: 2 } : { limit: 2, cursor },
      );
      pagedHashes.push(...page.rows.map((transaction) => transaction.transactionHash));
      cursor = page.nextCursor ?? undefined;
    } while (cursor !== undefined);

    assert.deepEqual(
      pagedHashes,
      expected.rows.map((transaction) => transaction.transactionHash),
    );
    assert.equal(new Set(pagedHashes).size, pagedHashes.length);

    const firstPage = queryTrades(transactions, { kind: "all" }, { limit: 2 });
    assert.notEqual(firstPage.nextCursor, null);
    if (firstPage.nextCursor === null) {
      throw new Error("测试交易页未生成下一页游标");
    }
    const nextCursor = firstPage.nextCursor;
    assert.throws(
      () => queryTrades(
        transactions,
        { kind: "account", account: "TST001" },
        { limit: 2, cursor: nextCursor },
      ),
      RangeError,
    );
    assert.throws(
      () => queryTrades(transactions, { kind: "all" }, { cursor: "not-a-cursor" }),
      RangeError,
    );
  });

  it("稳定分页已实现与现金流明细且完整合计不随页面变化", () => {
    const { transactions, fifo } = fixture();
    const allRealized = queryRealized(fifo.realizedRecords, { kind: "all" }, { limit: 200 });
    const realizedRows: string[] = [];
    let realizedCursor: string | undefined;
    do {
      const page = queryRealized(
        fifo.realizedRecords,
        { kind: "all" },
        realizedCursor === undefined
          ? { limit: 1 }
          : { limit: 1, cursor: realizedCursor },
      );
      realizedRows.push(...page.rows.map((row) => (
        `${row.account}:${row.openTransaction}:${row.closeTransaction}`
      )));
      assert.equal(page.totalCount, allRealized.totalCount);
      assert.equal(page.totalPnl, allRealized.totalPnl);
      realizedCursor = page.nextCursor ?? undefined;
    } while (realizedCursor !== undefined);
    assert.deepEqual(realizedRows, allRealized.rows.map((row) => (
      `${row.account}:${row.openTransaction}:${row.closeTransaction}`
    )));

    const allCashflow = queryCashflow(transactions, { kind: "all" }, { limit: 200 });
    const cashflowRows: string[] = [];
    let cashflowCursor: string | undefined;
    do {
      const page = queryCashflow(
        transactions,
        { kind: "all" },
        cashflowCursor === undefined
          ? { limit: 1 }
          : { limit: 1, cursor: cashflowCursor },
      );
      cashflowRows.push(...page.rows.map((row) => row.transactionHash));
      assert.equal(page.totalCount, allCashflow.totalCount);
      assert.equal(page.total, allCashflow.total);
      assert.deepEqual(page.byAction, allCashflow.byAction);
      cashflowCursor = page.nextCursor ?? undefined;
    } while (cashflowCursor !== undefined);
    assert.deepEqual(cashflowRows, allCashflow.rows.map((row) => row.transactionHash));

    const firstRealized = queryRealized(
      fifo.realizedRecords,
      { kind: "all" },
      { limit: 1 },
    );
    const realizedNextCursor = firstRealized.nextCursor;
    if (realizedNextCursor === null) {
      throw new Error("测试已实现明细页未生成下一页游标");
    }
    assert.throws(
      () => queryCashflow(transactions, { kind: "all" }, {
        limit: 1,
        cursor: realizedNextCursor,
      }),
      RangeError,
    );
  });

  it("拒绝无效日期范围和非正查询上限", () => {
    assert.throws(
      () => queryTrades([], { kind: "all" }, { limit: 0 }),
      (error: unknown) => error instanceof RangeError && error.message.includes("limit"),
    );
    assert.throws(
      () => queryCashflow([], { kind: "all" }, {
        fromDate: "2026-02-01",
        toDate: "2026-01-01",
      }),
      RangeError,
    );
    assert.throws(
      () => queryRealized([], { kind: "all" }, { fromDate: "not-a-date" }),
      RangeError,
    );
  });
});
