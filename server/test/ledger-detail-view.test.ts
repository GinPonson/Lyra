import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { Decimal } from "decimal.js";

import {
  createPublicCashflowQuery,
  createPublicExpiringPositions,
  createPublicPositionList,
  createPublicRealizedQuery,
  createPublicTradeQuery,
  resolveAccountScope,
} from "../src/application/index.js";
import type {
  ExpiringPositionsResult,
  PositionRow,
  RealizedQueryResult,
  TradeQueryResult,
} from "../src/domain/analytics/index.js";
import type { ParsedTransaction } from "../src/domain/ledger/index.js";

const scope = resolveAccountScope({ kind: "all" }, [
  {
    id: "account-uuid-1",
    legacyAccountKey: "TST001",
    baseCurrency: "USD",
    status: "active",
  },
]);

const position: PositionRow = {
  account: "TST001",
  symbolKey: "AAA",
  assetType: "stock",
  underlying: "AAA",
  expiry: null,
  strike: null,
  optionType: null,
  direction: "long",
  quantity: new Decimal("1.2500"),
  cost: new Decimal("12.3456"),
  firstOpenDate: "2026-08-01",
};

const transaction: ParsedTransaction = {
  account: "TST001",
  sourceType: "csv",
  sourceReference: "private-import-id",
  recordDate: "2026-08-02",
  transactionDate: "2026-08-02",
  action: "Buy",
  rawSymbol: "AAA",
  description: "AAA CORP",
  assetType: "stock",
  underlying: "AAA",
  expiry: null,
  strike: null,
  optionType: null,
  quantity: new Decimal("1.25"),
  price: new Decimal("9.8765"),
  fees: new Decimal("0.01"),
  amount: new Decimal("-12.355625"),
  sourceHash: "a".repeat(64),
  sequence: 7,
  occurrence: 1,
  businessHash: "b".repeat(64),
  transactionHash: "c".repeat(64),
};

describe("public ledger detail views", () => {
  it("映射账户 UUID、显式序列化 Decimal 并隔离内部哈希与来源引用", () => {
    assert.deepEqual(createPublicPositionList([position], scope), [{
      accountId: "account-uuid-1",
      symbolKey: "AAA",
      assetType: "stock",
      underlying: "AAA",
      expiry: null,
      strike: null,
      optionType: null,
      direction: "long",
      quantity: "1.25",
      cost: "12.3456",
      firstOpenDate: "2026-08-01",
    }]);

    const tradeResult: TradeQueryResult = {
      rows: [transaction],
      count: 1,
      nextCursor: "next-page",
    };
    const publicTrades = createPublicTradeQuery(tradeResult, scope);
    assert.equal(publicTrades.rows[0]?.price, "9.8765");
    assert.equal(publicTrades.rows[0]?.accountId, "account-uuid-1");
    const serializedTrades = JSON.stringify(publicTrades);
    for (const privateValue of [
      "TST001", "private-import-id", "a".repeat(64), "b".repeat(64), "c".repeat(64),
    ]) {
      assert.equal(serializedTrades.includes(privateValue), false);
    }

    const publicCashflow = createPublicCashflowQuery({
      rows: [{ ...transaction, assetType: "cash", action: "Credit Interest" }],
      count: 1,
      totalCount: 1,
      total: "1.0000",
      byAction: { "Credit Interest": "1.0000" },
      nextCursor: null,
    }, scope);
    assert.equal(publicCashflow.rows[0]?.amount, "-12.355625");
    assert.notEqual(publicCashflow.byAction, undefined);

    const realized: RealizedQueryResult = {
      rows: [{
        account: "TST001",
        symbolKey: "AAA",
        assetType: "stock",
        underlying: "AAA",
        direction: "long",
        openDate: "2026-08-01",
        closeDate: "2026-08-02",
        quantity: new Decimal("1.25"),
        openPrice: new Decimal("9.8765"),
        closePrice: new Decimal("10.5"),
        openFees: new Decimal("0.01"),
        closeFees: new Decimal("0.02"),
        pnl: new Decimal("0.749375"),
        closeAction: "Sell",
        openTransaction: "d".repeat(64),
        closeTransaction: "e".repeat(64),
      }],
      count: 1,
      totalCount: 1,
      totalPnl: "0.7494",
      nextCursor: null,
    };
    const publicRealized = createPublicRealizedQuery(realized, scope);
    assert.equal(publicRealized.rows[0]?.pnl, "0.749375");
    assert.equal(JSON.stringify(publicRealized).includes("d".repeat(64)), false);

    const expiring: ExpiringPositionsResult = {
      rows: [{
        ...position,
        assetType: "option",
        symbolKey: "AAA 09/18/2026 10 C",
        expiry: "2026-09-18",
        strike: new Decimal("10"),
        optionType: "C",
        status: "upcoming",
        daysRemaining: 18,
        premiumDollars: new Decimal("1234.56"),
      }],
      count: 1,
      asOf: "2026-08-31",
      cutoff: "2026-09-30",
      expired: 0,
      expiresToday: 0,
      upcoming: 1,
    };
    assert.equal(
      createPublicExpiringPositions(expiring, scope).rows[0]?.premiumDollars,
      "1234.56",
    );
  });

  it("拒绝把授权范围外的领域查询行投影给 App", () => {
    assert.throws(
      () => createPublicTradeQuery({
        rows: [{ ...transaction, account: "TST999" }],
        count: 1,
        nextCursor: null,
      }, scope),
      /授权范围外账户/,
    );
  });
});
