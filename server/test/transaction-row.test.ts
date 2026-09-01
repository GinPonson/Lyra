import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { Decimal } from "decimal.js";

import {
  IngestError,
  parseTransactionRow,
  withOccurrence,
} from "../src/domain/ledger/index.js";
import type {
  ParseTransactionContext,
  RawTransactionFields,
} from "../src/domain/ledger/index.js";

const CONTEXT: ParseTransactionContext = {
  account: "TST001",
  sourceHash: "source-digest",
  sourceType: "csv",
  sourceReference: "import-1",
  sequence: 1,
  where: "sample.csv 第1行",
};

/** 构造具有完整八列的交易行，测试只覆盖当前用例需要修改的字段。 */
function row(overrides: Partial<RawTransactionFields> = {}): RawTransactionFields {
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

describe("parseTransactionRow", () => {
  it("分类股票、期权和现金交易", () => {
    const stock = parseTransactionRow(row(), CONTEXT);
    assert.equal(stock.assetType, "stock");
    assert.equal(stock.sourceType, "csv");
    assert.equal(stock.sourceReference, "import-1");
    assert.equal(stock.underlying, "INTC");
    assert.equal(stock.transactionDate, "2026-07-28");

    const option = parseTransactionRow(row({
      Action: "Buy to Open",
      Symbol: "GOOG 03/19/2027 385.00 C",
      Description: "CALL ALPHABET",
      Quantity: "1",
      Price: "$18.65",
      "Fees & Comm": "$0.66",
      Amount: "-$1865.66",
    }), CONTEXT);
    assert.equal(option.assetType, "option");
    assert.equal(option.underlying, "GOOG");
    assert.equal(option.expiry, "2027-03-19");
    assert.equal(option.strike?.equals(new Decimal("385.00")), true);
    assert.equal(option.optionType, "C");

    const cash = parseTransactionRow(row({
      Date: "05/13/2026 as of 05/12/2026",
      Action: "MoneyLink Transfer",
      Symbol: "",
      Description: "Tfr WISE",
      Quantity: "",
      Price: "",
      "Fees & Comm": "",
      Amount: "$5900.00",
    }), CONTEXT);
    assert.equal(cash.assetType, "cash");
    assert.equal(cash.underlying, null);
    assert.equal(cash.transactionDate, "2026-05-12");
  });

  it("拒绝未知动作", () => {
    assert.throws(
      () => parseTransactionRow(row({ Action: "Wheel Deal" }), CONTEXT),
      (error: unknown) => error instanceof IngestError && error.message.includes("未知 Action"),
    );
  });

  it("拒绝金额勾稽不符的持仓交易", () => {
    assert.throws(
      () => parseTransactionRow(row({ Amount: "-$999.99" }), CONTEXT),
      (error: unknown) => error instanceof IngestError && error.message.includes("金额勾稽不符"),
    );
  });

  it("使用 100 倍期权乘数完成卖出金额勾稽", () => {
    const transaction = parseTransactionRow(row({
      Date: "07/15/2026",
      Action: "Sell to Close",
      Symbol: "WMT 03/19/2027 125.00 C",
      Description: "CALL WALMART",
      Quantity: "2",
      Price: "$7.63",
      "Fees & Comm": "$1.37",
      Amount: "$1524.63",
    }), CONTEXT);
    assert.equal(transaction.assetType, "option");
  });

  it("拒绝缺少数量、价格或期权合约的持仓交易", () => {
    assert.throws(() => parseTransactionRow(row({ Quantity: "" }), CONTEXT), IngestError);
    assert.throws(() => parseTransactionRow(row({ Price: "" }), CONTEXT), IngestError);
    assert.throws(
      () => parseTransactionRow(row({ Action: "Buy to Open", Symbol: "INTC" }), CONTEXT),
      IngestError,
    );
  });

  it("允许 Schwab 到期记录缺少价格和金额", () => {
    const transaction = parseTransactionRow(row({
      Action: "Expired",
      Symbol: "INTC 05/15/2026 55.00 P",
      Quantity: "1",
      Price: "",
      "Fees & Comm": "",
      Amount: "",
    }), CONTEXT);
    assert.equal(transaction.assetType, "option");
    assert.equal(transaction.amount, null);
  });

  it("使用业务字段和出现次数生成稳定哈希", () => {
    const first = parseTransactionRow(row(), CONTEXT);
    const second = withOccurrence(first, 2);

    // 期望值由现有 Python `ParsedTxn.business_hash/txn_hash` 生成，作为跨语言 golden。
    assert.equal(first.businessHash, "555683d4fa86a714c7ec2a5af781c4fad43e2ed673e690ed271be2ac012dc0ea");
    assert.equal(first.transactionHash, "9954a5fc56b5737d6db4e0ed0a942814135c10c416426acc9f24122cb3eeed08");
    assert.equal(second.businessHash, first.businessHash);
    assert.equal(second.transactionHash, "67437c54ef5f8aad73ae8b73916f87245b54737c1b9c4c829b96937d28958334");
    assert.notEqual(first.transactionHash, second.transactionHash);
  });
});
