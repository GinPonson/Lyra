import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { Decimal } from "decimal.js";

import {
  reconcileEmailTrades,
  resolveEmailAccount,
} from "../src/domain/gmail/index.js";
import type { EmailTrade } from "../src/domain/gmail/index.js";
import {
  IngestError,
  normalizeTransactionBatch,
} from "../src/domain/ledger/index.js";

/** 构造一笔可用于重复匹配验证的标准邮件期权成交。 */
function optionTrade(): EmailTrade {
  return {
    tradeDate: "2026-07-23",
    action: "Sell to Open",
    symbol: "INTC 07/31/2026 120.00 C",
    description: "CALL INTEL CORP $120 EXP 07/31/26",
    quantity: new Decimal("1"),
    price: new Decimal("2.29"),
    fees: new Decimal("0.66"),
    amount: new Decimal("228.34"),
  };
}

describe("resolveEmailAccount", () => {
  it("接受匹配的显式账户，并拒绝尾号不一致", () => {
    assert.equal(resolveEmailAccount("276", [], "TST276"), "TST276");
    assert.throws(
      () => resolveEmailAccount("276", [], "TST999"),
      (error: unknown) => error instanceof IngestError && error.message.includes("不匹配"),
    );
  });

  it("仅在现有账户候选唯一时自动解析", () => {
    assert.equal(resolveEmailAccount("276", ["IRA111", "TST276", "TST276"], null), "TST276");
    assert.throws(
      () => resolveEmailAccount("276", ["IRA111"], null),
      (error: unknown) => error instanceof IngestError && error.message.includes("没有尾号"),
    );
    assert.throws(
      () => resolveEmailAccount("276", ["IRA276", "TST276"], null),
      (error: unknown) => error instanceof IngestError && error.message.includes("多个尾号"),
    );
  });
});

describe("reconcileEmailTrades", () => {
  it("按出现次数消费重复成交，并区分冲突与缺失", () => {
    const trade = optionTrade();
    const preview = normalizeTransactionBatch({
      fileName: "Individual_TST276_Transactions.csv",
      account: "TST276",
      sourceHash: "source-hash",
      sourceType: "gmail",
      sourceReference: "gmail-message-reconciliation",
      rows: [
        {
          Date: "07/23/2026",
          Action: "Sell to Open",
          Symbol: trade.symbol,
          Description: trade.description,
          Quantity: "1",
          Price: "$2.29",
          "Fees & Comm": "$0.66",
          Amount: "$228.34",
        },
        {
          Date: "07/23/2026",
          Action: "Sell to Open",
          Symbol: trade.symbol,
          Description: trade.description,
          Quantity: "1",
          Price: "$2.29",
          "Fees & Comm": "$0.66",
          Amount: "$228.34",
        },
      ],
    });
    const missing: EmailTrade = {
      tradeDate: "2026-07-23",
      action: "Buy",
      symbol: "TSLA",
      description: "TESLA INC",
      quantity: new Decimal("1"),
      price: new Decimal("10"),
      fees: new Decimal("0"),
      amount: new Decimal("-10"),
    };

    const result = reconcileEmailTrades([
      {
        source: "gmail-batch-1",
        account: "TST276",
        trades: [trade, trade, trade, missing],
      },
    ], preview.transactions);

    assert.deepEqual(result.rows.map((row) => row.status), [
      "matched",
      "matched",
      "conflict",
      "missing",
    ]);
    assert.equal(result.matched, 2);
    assert.equal(result.conflict, 1);
    assert.equal(result.missing, 1);
    assert.equal(result.rows[2]?.candidates, 2);
    assert.equal(result.readOnly, true);
    assert.equal(preview.transactions.length, 2);
  });
});
