import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { setImmediate } from "node:timers/promises";

import { Decimal } from "decimal.js";

import { resolveAccountScope } from "../src/application/account-scope.js";
import { createMarketPositionView } from "../src/application/market-position-view.js";
import { aggregatePositions } from "../src/domain/analytics/positions.js";
import type { PositionRow } from "../src/domain/analytics/types.js";
import { replayFifo } from "../src/domain/fifo/replay.js";
import { normalizeTransactionBatch } from "../src/domain/ledger/batch.js";
import type { RawTransactionFields } from "../src/domain/ledger/types.js";
import {
  marketInstrumentKey,
  type MarketDataReader,
  type MarketDataStatus,
  type MarketInstrument,
  type MarketQuoteLookup,
} from "../src/domain/market-data/types.js";

const scope = resolveAccountScope({ kind: "all" }, [
  { id: "usd-a", legacyAccountKey: "PRIVATE-USD-A", baseCurrency: "USD", status: "active" },
  { id: "usd-b", legacyAccountKey: "PRIVATE-USD-B", baseCurrency: "USD", status: "active" },
  { id: "eur-a", legacyAccountKey: "PRIVATE-EUR-A", baseCurrency: "EUR", status: "active" },
]);
const status: MarketDataStatus = {
  enabled: true,
  routes: { stock: ["fixture"], option: ["fixture"] },
  providers: [{ id: "fixture", assetTypes: ["stock", "option"], cacheTtlSeconds: 30 }],
};
const NOW = "2026-10-11T10:00:00.000Z";

function stock(overrides: Partial<PositionRow> = {}): PositionRow {
  return {
    account: "PRIVATE-USD-A",
    symbolKey: "INTC",
    assetType: "stock",
    underlying: "INTC",
    expiry: null,
    strike: null,
    optionType: null,
    direction: "long",
    quantity: new Decimal(10),
    cost: new Decimal(400),
    firstOpenDate: "2026-01-05",
    ...overrides,
  };
}

function option(overrides: Partial<PositionRow> = {}): PositionRow {
  return stock({
    symbolKey: "INTC 12/18/2026 50.000 C",
    assetType: "option",
    expiry: "2026-12-18",
    strike: new Decimal("50.000"),
    optionType: "C",
    quantity: new Decimal(2),
    cost: new Decimal(4),
    ...overrides,
  });
}

function available(instrument: MarketInstrument, price = "45", asOf = NOW): MarketQuoteLookup {
  return {
    status: "available",
    quote: {
      instrumentKey: marketInstrumentKey(instrument),
      providerId: "fixture",
      feed: "fixture delayed feed",
      currency: "USD",
      bid: null, ask: null, close: null,
      last: { value: price, asOf },
      timing: { kind: "delayed", delayMinutes: 15 },
      fetchedAt: NOW,
    },
    referencePrice: { value: price, asOf, basis: "last" },
    issue: null,
    cached: false,
  };
}

function readerFor(lookup: (instrument: MarketInstrument) => MarketQuoteLookup | Promise<MarketQuoteLookup>) {
  const calls: MarketInstrument[] = [];
  const reader: MarketDataReader = {
    status: () => status,
    getQuote: async (instrument) => { calls.push(instrument); return lookup(instrument); },
  };
  return { reader, calls };
}

function transaction(overrides: Partial<RawTransactionFields>): RawTransactionFields {
  return {
    Date: "01/05/2026", Action: "Buy", Symbol: "INTC", Description: "INTEL CORP",
    Quantity: "10", Price: "$40.00", "Fees & Comm": "$1.00", Amount: "-$401.00",
    ...overrides,
  };
}

describe("market position public view", () => {
  it("values real FIFO stock and long/short options without changing historical cost or mixing fees", async () => {
    const transactions = normalizeTransactionBatch({
      fileName: "Individual_TST001_Transactions.csv", account: "PRIVATE-USD-A",
      sourceHash: "fixture-hash", sourceType: "csv", sourceReference: "fixture-reference",
      rows: [
        transaction({}),
        transaction({ Action: "Buy to Open", Symbol: "INTC 12/18/2026 50.000 C", Quantity: "2", Price: "$2.00" }),
        transaction({
          Action: "Sell to Open", Symbol: "AMD 12/18/2026 200 P", Quantity: "1",
          Price: "$3.00", "Fees & Comm": "$0.65", Amount: "$299.35",
        }),
      ],
    }).transactions;
    const fifo = replayFifo(transactions);
    const positions = aggregatePositions(fifo.openLots, { kind: "all" });
    const before = JSON.stringify({ positions, fifo, transactions });
    const { reader, calls } = readerFor((instrument) => instrument.assetType === "stock"
      ? available(instrument, "45", "2026-10-11T10:00:00Z")
      : instrument.optionType === "C"
        ? available(instrument, "2.5", "2026-10-11T10:01:00Z")
        : available(instrument, "4", "2026-10-11T09:59:00Z"));
    const result = await createMarketPositionView(positions, scope, reader);
    const stockRow = result.rows.find((row) => row.assetType === "stock")!;
    const longCall = result.rows.find((row) => row.optionType === "C")!;
    const shortPut = result.rows.find((row) => row.optionType === "P")!;
    assert.deepEqual(stockRow.valuation, {
      marketValue: "450", unrealizedPnl: "50", multiplier: "1", basis: "reference_price_excluding_fees",
    });
    assert.deepEqual(longCall.valuation, {
      marketValue: "500", unrealizedPnl: "100", multiplier: "100", basis: "reference_price_excluding_fees",
    });
    assert.deepEqual(shortPut.valuation, {
      marketValue: "-400", unrealizedPnl: "-100", multiplier: "100", basis: "reference_price_excluding_fees",
    });
    assert.equal(longCall.cost, "4");
    assert.equal(longCall.instrumentKey, "US:option:INTC:2026-12-18:C:50");
    assert.equal(result.count, 3);
    assert.equal(result.pricedCount, 3);
    assert.equal(result.complete, true);
    assert.equal(result.basis, "current_positions_at_quote_times_excluding_fees");
    assert.deepEqual(result.marketData, status);
    assert.deepEqual(result.currencyGroups.find((group) => group.currency === "USD"), {
      currency: "USD", accountIds: ["usd-a", "usd-b"], positions: 3, pricedPositions: 3,
      complete: true, marketValue: "550", unrealizedPnl: "50",
      asOfFrom: "2026-10-11T09:59:00.000Z", asOfTo: "2026-10-11T10:01:00.000Z",
    });
    assert.equal(calls.length, 3);
    assert.doesNotMatch(JSON.stringify(result), /PRIVATE-|fixture-reference|fixture-hash|cashBalance/u);
    assert.equal(JSON.stringify({ positions, fifo, transactions }), before);
  });

  it("keeps fractional stock precision and preserves a reader-selected mid instead of choosing another price", async () => {
    const position = stock({ quantity: new Decimal("1.2500"), cost: new Decimal("12.3456") });
    const { reader } = readerFor((instrument) => {
      const lookup = available(instrument, "999");
      return { ...lookup, referencePrice: { value: "10.123456", asOf: NOW, basis: "mid" } };
    });
    const result = await createMarketPositionView([position], scope, reader);
    assert.equal(result.rows[0]?.valuation.marketValue, "12.65432");
    assert.equal(result.rows[0]?.valuation.unrealizedPnl, "0.30872");
    assert.equal(result.rows[0]?.cost, "12.3456");
    assert.equal(result.rows[0]?.quantity, "1.25");
  });

  it("deduplicates instruments across authorized accounts and leaves non-USD holdings unpriced", async () => {
    const { reader, calls } = readerFor((instrument) => available(instrument));
    const result = await createMarketPositionView([
      stock({ symbolKey: "intc", underlying: "intc" }),
      stock({ account: "PRIVATE-USD-B", quantity: new Decimal(1), cost: new Decimal(40) }),
      stock({ account: "PRIVATE-EUR-A" }),
    ], scope, reader);
    assert.deepEqual(calls, [{ assetType: "stock", market: "US", symbol: "INTC", currency: "USD" }]);
    assert.equal(result.pricedCount, 2);
    assert.equal(result.complete, false);
    const eur = result.rows.find((row) => row.currency === "EUR")!;
    assert.equal(eur.quote.issue, "currency_unsupported");
    assert.equal(eur.instrumentKey, null);
    assert.equal(eur.valuation.marketValue, null);
    assert.equal(result.currencyGroups[0]?.marketValue, null);
    assert.equal(result.currencyGroups[1]?.marketValue, "495");
    assert.equal(result.currencyGroups[1]?.unrealizedPnl, "55");
    assert.equal(result.currencyGroups[1]?.complete, true);
  });

  it("permits stale row references but suppresses complete totals even without a provider error", async () => {
    const { reader } = readerFor((instrument) => ({
      ...available(instrument, "43", "2026-09-30T10:00:00Z"),
      status: "stale", issue: null, cached: true,
    }));
    const result = await createMarketPositionView([stock()], scope, reader);
    assert.equal(result.pricedCount, 1);
    assert.equal(result.complete, false);
    assert.equal(result.rows[0]?.valuation.marketValue, "430");
    assert.equal(result.rows[0]?.valuation.unrealizedPnl, "30");
    assert.equal(result.rows[0]?.quote.status, "stale");
    const group = result.currencyGroups.find((item) => item.currency === "USD")!;
    assert.equal(group.pricedPositions, 1);
    assert.equal(group.complete, false);
    assert.equal(group.marketValue, null);
    assert.equal(group.unrealizedPnl, null);
    assert.equal(group.asOfFrom, "2026-09-30T10:00:00.000Z");
  });

  it("distinguishes explicitly quoted zero from missing quotes and incomplete portfolio coverage", async () => {
    const { reader } = readerFor((instrument) => instrument.assetType === "option"
      ? available(instrument, "0")
      : { status: "unavailable", quote: null, referencePrice: null, issue: "no_data", cached: false });
    const result = await createMarketPositionView([stock(), option()], scope, reader);
    assert.equal(result.rows[0]?.valuation.marketValue, null);
    assert.equal(result.rows[0]?.valuation.unrealizedPnl, null);
    assert.equal(result.rows[1]?.valuation.marketValue, "0");
    assert.equal(result.rows[1]?.valuation.unrealizedPnl, "-400");
    assert.equal(result.pricedCount, 1);
    assert.equal(result.complete, false);
    assert.equal(result.currencyGroups.find((group) => group.currency === "USD")?.marketValue, null);
  });

  it("rejects ambiguous or nonstandard identities before requesting any quotes", async () => {
    const { reader, calls } = readerFor((instrument) => available(instrument));
    const result = await createMarketPositionView([
      stock({ symbolKey: null }),
      stock({ underlying: "AMD" }),
      stock({ symbolKey: "$INTC", underlying: "$INTC" }),
      stock({ expiry: "2026-12-18" }),
      option({ symbolKey: null }),
      option({ strike: null }),
      option({ expiry: "2026-02-30" }),
      option({ expiry: "2026-12-19" }),
      option({ optionType: "P" }),
      option({ symbolKey: "AMD 12/18/2026 50 C" }),
      option({ symbolKey: "INTC1 12/18/2026 50 C", underlying: "INTC1" }),
      option({ symbolKey: "BRK.B 12/18/2026 50 C", underlying: "BRK.B" }),
      option({ symbolKey: "INTC 12/18/2026 50.0001 C", strike: new Decimal("50.0001") }),
      option({ quantity: new Decimal("0.5") }),
    ], scope, reader);
    assert.equal(calls.length, 0);
    assert.equal(result.pricedCount, 0);
    assert.equal(result.complete, false);
    for (const row of result.rows) {
      assert.equal(row.quote.issue, "unsupported_instrument");
      assert.equal(row.instrumentKey, null);
      assert.equal(row.valuation.marketValue, null);
      assert.equal(row.valuation.multiplier, null);
    }
  });

  it("normalizes valid stock class symbols and standard option strike identities without changing public ledger fields", async () => {
    const { reader, calls } = readerFor((instrument) => available(instrument, "1"));
    const result = await createMarketPositionView([
      stock({ symbolKey: "brk.b", underlying: "brk.b" }),
      stock({ symbolKey: "brk-b", underlying: "brk-b" }),
      option(),
      option({ account: "PRIVATE-USD-B", symbolKey: "INTC 12/18/2026 50 C", strike: new Decimal(50) }),
    ], scope, reader);
    assert.equal(calls.length, 3);
    assert.equal(result.rows[0]?.instrumentKey, "US:stock:BRK.B");
    assert.equal(result.rows[1]?.instrumentKey, "US:stock:BRK-B");
    assert.equal(result.rows[0]?.symbolKey, "brk.b");
    assert.equal(result.rows[2]?.instrumentKey, result.rows[3]?.instrumentKey);
  });

  it("rejects all account and amount inconsistencies before starting external reads", async () => {
    const { reader, calls } = readerFor((instrument) => available(instrument));
    await assert.rejects(createMarketPositionView([
      stock(), stock({ account: "PRIVATE-UNKNOWN" }),
    ], scope, reader), /授权范围外账户/u);
    await assert.rejects(createMarketPositionView([stock()], { ...scope, currencyGroups: [] }, reader), /币种分组不完整/u);
    for (const quantity of [new Decimal(0), new Decimal(-1), new Decimal(NaN)]) {
      await assert.rejects(createMarketPositionView([stock({ quantity })], scope, reader), /持仓数量/u);
    }
    assert.equal(calls.length, 0);
  });

  it("does not value another instrument, currency, invalid reference or unexpected reader error", async () => {
    const invalidLookups: ((instrument: MarketInstrument) => MarketQuoteLookup)[] = [
      (instrument) => { const lookup = available(instrument); lookup.quote!.instrumentKey = "US:stock:AMD"; return lookup; },
      (instrument) => {
        const lookup = available(instrument);
        (lookup.quote as unknown as { currency: string }).currency = "EUR";
        return lookup;
      },
      (instrument) => ({ ...available(instrument), referencePrice: null }),
      (instrument) => available(instrument, "NaN"),
      (instrument) => available(instrument, "-1"),
      (instrument) => available(instrument, "1", "2026-10-11T10:00:00"),
    ];
    for (const invalid of invalidLookups) {
      const { reader } = readerFor(invalid);
      const result = await createMarketPositionView([stock()], scope, reader);
      assert.equal(result.rows[0]?.quote.issue, "invalid_response");
      assert.equal(result.rows[0]?.valuation.marketValue, null);
      assert.equal(result.complete, false);
    }
    const { reader } = readerFor(() => { throw new Error("provider-url-with-private-api-key"); });
    const failed = await createMarketPositionView([stock()], scope, reader);
    assert.equal(failed.rows[0]?.quote.issue, "provider_unavailable");
    assert.doesNotMatch(JSON.stringify(failed), /private-api-key/u);
  });

  it("limits quote concurrency to four and shares one lookup per instrument across accounts", async () => {
    let active = 0;
    let maxActive = 0;
    const { reader, calls } = readerFor(async (instrument) => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      await setImmediate();
      active -= 1;
      return available(instrument, "10");
    });
    const positions = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"].flatMap((symbol) => [
      stock({ symbolKey: symbol, underlying: symbol, quantity: new Decimal(1), cost: new Decimal(8) }),
      stock({ account: "PRIVATE-USD-B", symbolKey: symbol, underlying: symbol, quantity: new Decimal(1), cost: new Decimal(8) }),
    ]);
    const result = await createMarketPositionView(positions, scope, reader);
    assert.equal(maxActive, 4);
    assert.equal(active, 0);
    assert.equal(calls.length, 8);
    assert.equal(result.count, 16);
    assert.equal(result.complete, true);
    assert.equal(result.currencyGroups.find((group) => group.currency === "USD")?.marketValue, "160");
    assert.equal(result.currencyGroups.find((group) => group.currency === "USD")?.unrealizedPnl, "32");
  });

  it("returns true empty-position totals without inventing prices or querying a disabled reader", async () => {
    const result = await createMarketPositionView([], scope, {
      status: () => ({ enabled: false, routes: { stock: [], option: [] }, providers: [] }),
      getQuote: async () => { throw new Error("empty positions must not query"); },
    });
    assert.equal(result.count, 0);
    assert.equal(result.pricedCount, 0);
    assert.equal(result.complete, true);
    assert.equal(result.marketData.enabled, false);
    for (const group of result.currencyGroups) {
      assert.equal(group.positions, 0);
      assert.equal(group.pricedPositions, 0);
      assert.equal(group.complete, true);
      assert.equal(group.marketValue, "0");
      assert.equal(group.unrealizedPnl, "0");
      assert.equal(group.asOfFrom, null);
      assert.equal(group.asOfTo, null);
    }
  });
});
