import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { MarketDataService, type MarketDataServiceOptions } from "../src/application/market-data-service.js";
import { selectMarketReferencePrice } from "../src/domain/market-data/reference-price.js";
import {
  marketInstrumentKey,
  type MarketDataProvider,
  type MarketInstrument,
  type MarketQuote,
  type ProviderQuoteResult,
} from "../src/domain/market-data/types.js";

const NOW = new Date("2026-10-09T20:00:00.000Z");
const QUOTE_TIME = "2026-10-09T19:40:00.000Z";
const stock = (symbol = "AMD"): MarketInstrument => ({ assetType: "stock", market: "US", symbol, currency: "USD" });
const option: MarketInstrument = {
  assetType: "option", market: "US", underlying: "AMD", expiry: "2026-10-16",
  strike: "150", optionType: "C", multiplier: "100", currency: "USD",
};

function quote(instrument: MarketInstrument, providerId = "first", overrides: Partial<MarketQuote> = {}): MarketQuote {
  return {
    instrumentKey: marketInstrumentKey(instrument), providerId, feed: "consolidated",
    currency: "USD", bid: { value: "100.01", asOf: QUOTE_TIME }, ask: { value: "100.04", asOf: QUOTE_TIME },
    last: null, close: null, timing: { kind: "delayed", delayMinutes: 15 }, fetchedAt: NOW.toISOString(), ...overrides,
  };
}

function provider(id: string, fetchQuote: MarketDataProvider["fetchQuote"]): MarketDataProvider {
  return { id, assetTypes: ["stock", "option"], fetchQuote };
}

function service(providers: readonly MarketDataProvider[], overrides: Partial<MarketDataServiceOptions> = {}): MarketDataService {
  return new MarketDataService({
    providers,
    routes: { stock: providers.map((entry) => entry.id), option: providers.map((entry) => entry.id) },
    policies: Object.fromEntries(providers.map((entry) => [entry.id, {
      cacheTtlMs: 60_000, maxRequestsPerMinute: 100, maxRequestsPerDay: 100,
    }])),
    now: () => NOW,
    ...overrides,
  });
}

describe("provider-neutral market data service", () => {
  it("coalesces concurrent reads and caches a quote without changing its market timestamp", async () => {
    let calls = 0;
    const source = provider("first", async (instrument) => {
      calls += 1;
      return { status: "available", quote: quote(instrument) };
    });
    const data = service([source]);
    const results = await Promise.all(Array.from({ length: 20 }, () => data.getQuote(stock())));
    assert.equal(calls, 1);
    assert.ok(results.every((result) => result.referencePrice?.value === "100.025"));
    const again = await data.getQuote(stock());
    assert.equal(again.cached, true);
    assert.equal(again.referencePrice?.asOf, QUOTE_TIME);
    assert.equal(again.quote?.fetchedAt, NOW.toISOString());
    assert.equal(calls, 1);
    // A consumer cannot mutate the shared provider cache.
    again.quote!.bid!.value = "999";
    assert.equal((await data.getQuote(stock())).referencePrice?.value, "100.025");
  });

  it("keeps routing, instrument identity and price policy independent of vendor names", async () => {
    const calls: string[] = [];
    const sources = ["first", "replacement"].map((id) => provider(id, async (instrument) => {
      calls.push(`${id}:${marketInstrumentKey(instrument)}`);
      return { status: "available", quote: quote(instrument, id) };
    }));
    const data = service(sources, { routes: { stock: ["first"], option: ["replacement"] } });
    const [share, contract] = await Promise.all([data.getQuote(stock()), data.getQuote(option)]);
    assert.equal(share.quote?.providerId, "first");
    assert.equal(contract.quote?.providerId, "replacement");
    assert.deepEqual(share.referencePrice, contract.referencePrice);
    assert.notEqual(share.quote?.instrumentKey, contract.quote?.instrumentKey);
    assert.equal(calls.length, 2);
    assert.deepEqual(data.status().routes, { stock: ["first"], option: ["replacement"] });
  });

  it("returns a failed refresh as stale and preserves price time instead of returning zero", async () => {
    let clock = NOW.getTime();
    let calls = 0;
    const source = provider("first", async (instrument) => {
      calls += 1;
      if (calls > 1) throw new Error("secret-token-and-private-upstream-url");
      return { status: "available", quote: quote(instrument) };
    });
    const data = service([source], { now: () => new Date(clock) });
    await data.getQuote(stock());
    clock += 61_000;
    const stale = await data.getQuote(stock());
    assert.equal(stale.status, "stale");
    assert.equal(stale.issue, "provider_unavailable");
    assert.equal(stale.referencePrice?.value, "100.025");
    assert.equal(stale.referencePrice?.asOf, QUOTE_TIME);
    assert.equal(JSON.stringify(stale).includes("secret-token"), false);
    assert.equal((await data.getQuote(stock())).status, "stale");
    assert.equal(calls, 2);
  });

  it("uses only explicitly configured fallbacks and does not let a stale primary mask a healthy one", async () => {
    let clock = NOW.getTime();
    let primaryFailed = false;
    let backupCalls = 0;
    const primary = provider("first", async (instrument) => primaryFailed
      ? { status: "unavailable", reason: "rate_limited" }
      : { status: "available", quote: quote(instrument) });
    const backup = provider("backup", async (instrument) => {
      backupCalls += 1;
      return { status: "available", quote: quote(instrument, "backup") };
    });
    const data = service([primary, backup], { now: () => new Date(clock) });
    assert.equal((await data.getQuote(stock())).quote?.providerId, "first");
    assert.equal(backupCalls, 0);
    primaryFailed = true;
    clock += 61_000;
    const recovered = await data.getQuote(stock());
    assert.equal(recovered.status, "available");
    assert.equal(recovered.quote?.providerId, "backup");
    const noFallback = service([primary, backup], { routes: { stock: ["first"], option: [] } });
    assert.equal((await noFallback.getQuote(stock())).status, "unavailable");
    assert.equal(backupCalls, 1);
  });

  it("does not query remote providers when disabled, even if implementations are registered", async () => {
    let calls = 0;
    const data = service([provider("first", async () => { calls += 1; throw new Error("must not run"); })], {
      routes: { stock: [], option: [] },
    });
    assert.equal(data.status().enabled, false);
    assert.deepEqual(await data.getQuote(stock()), {
      status: "unavailable", quote: null, referencePrice: null, issue: "not_configured", cached: false,
    });
    assert.equal(calls, 0);
  });

  it("enforces a rolling request budget before fetching and bounds cache growth", async () => {
    let calls = 0;
    const data = service([provider("first", async (instrument) => {
      calls += 1;
      return { status: "available", quote: quote(instrument) };
    })], {
      policies: { first: { cacheTtlMs: 60_000, maxRequestsPerMinute: 10, maxRequestsPerDay: 2 } },
      maxCacheEntries: 1,
    });
    await data.getQuote(stock("AMD"));
    await data.getQuote(stock("INTC"));
    assert.equal((await data.getQuote(stock("QCOM"))).issue, "rate_limited");
    // AMD was evicted; a bounded cache cannot bypass the request budget on a refetch.
    assert.equal((await data.getQuote(stock("AMD"))).issue, "rate_limited");
    assert.equal(calls, 2);
  });

  it("applies a provider-wide cooldown for rejected credentials without blocking other providers", async () => {
    let badCalls = 0;
    let goodCalls = 0;
    const data = service([
      provider("first", async () => { badCalls += 1; return { status: "unavailable", reason: "credentials_rejected" }; }),
      provider("other", async (instrument) => { goodCalls += 1; return { status: "available", quote: quote(instrument, "other") }; }),
    ], { concurrency: 1 });
    await Promise.all([data.getQuote(stock("AMD")), data.getQuote(stock("INTC"))]);
    assert.equal(badCalls, 1);
    assert.equal(goodCalls, 2);
  });

  it("bounds a provider that ignores AbortSignal and aborts its request on timeout", async () => {
    let signal: AbortSignal | undefined;
    const data = service([provider("first", async (_instrument, context) => {
      signal = context.signal;
      return new Promise<ProviderQuoteResult>(() => {});
    })], { timeoutMs: 20 });
    const result = await data.getQuote(stock());
    assert.equal(result.issue, "timeout");
    assert.equal(signal?.aborted, true);
  });

  it("rejects mismatched identifiers, currency, timestamps and invalid numeric prices at the boundary", async () => {
    const invalid: Partial<MarketQuote>[] = [
      { instrumentKey: marketInstrumentKey(stock("OTHER")) },
      { providerId: "wrong" },
      { currency: "EUR" as "USD" },
      { bid: { value: "NaN", asOf: QUOTE_TIME } },
      { ask: { value: "-1", asOf: QUOTE_TIME } },
      { bid: { value: "100", asOf: "2027-01-01T00:00:00.000Z" } },
      { bid: { value: "100", asOf: "2026-10-09T19:40:00" } },
      { bid: { value: "100", asOf: "2026-02-31T19:40:00Z" } },
      { bid: { value: "200", asOf: QUOTE_TIME } },
      { bid: null, ask: null, last: { value: "0", asOf: QUOTE_TIME } },
    ];
    for (const overrides of invalid) {
      const data = service([provider("first", async (instrument) => ({
        status: "available", quote: quote(instrument, "first", overrides),
      }))]);
      const result = await data.getQuote(stock());
      assert.equal(result.status, "unavailable");
      assert.equal(result.issue, "invalid_response");
      assert.equal(result.referencePrice, null);
    }
  });

  it("distinguishes an old market price from a newly fetched quote", async () => {
    const old = "2026-09-01T20:00:00.000Z";
    const data = service([provider("first", async (instrument) => ({
      status: "available", quote: quote(instrument, "first", {
        bid: { value: "1", asOf: old }, ask: { value: "2", asOf: old },
      }),
    }))]);
    const result = await data.getQuote(stock());
    assert.equal(result.status, "stale");
    assert.equal(result.quote?.fetchedAt, NOW.toISOString());
    assert.equal(result.referencePrice?.asOf, old);
  });

  it("isolates adapter-owned objects and discards unknown fields and unsafe failure messages", async () => {
    const original = { ...quote(stock()), apiKey: "private-provider-key" };
    const data = service([provider("first", async () => ({ status: "available", quote: original }))]);
    const first = await data.getQuote(stock());
    assert.doesNotMatch(JSON.stringify(first), /apiKey|private-provider-key/);
    original.instrumentKey = marketInstrumentKey(stock("WRONG"));
    original.bid!.value = "999";
    const cached = await data.getQuote(stock());
    assert.equal(cached.quote?.instrumentKey, marketInstrumentKey(stock()));
    assert.equal(cached.referencePrice?.value, "100.025");
    const unsafe = service([provider("first", async () => ({
      status: "unavailable", reason: "unsafe-private-provider-detail",
    } as unknown as ProviderQuoteResult))]);
    const rejected = await unsafe.getQuote(stock());
    assert.equal(rejected.issue, "invalid_response");
    assert.doesNotMatch(JSON.stringify(rejected), /unsafe-private/);
  });

  it("uses one exact price policy, with matching bid/ask times and explicit fallback basis", () => {
    const midpoint = selectMarketReferencePrice(quote(stock()));
    assert.deepEqual(midpoint, { value: "100.025", asOf: QUOTE_TIME, basis: "mid" });
    const mismatched = quote(stock(), "first", {
      ask: { value: "100.04", asOf: "2026-10-09T19:41:00.000Z" },
      last: { value: "100.02", asOf: QUOTE_TIME },
      close: { value: "99.99", asOf: QUOTE_TIME },
    });
    assert.equal(selectMarketReferencePrice(mismatched)?.basis, "last");
    mismatched.last = null;
    assert.equal(selectMarketReferencePrice(mismatched)?.basis, "close");
    mismatched.close = null;
    assert.equal(selectMarketReferencePrice(mismatched), null);
    const precise = quote(stock(), "first", {
      bid: { value: "1234567890123456.123456789012", asOf: QUOTE_TIME },
      ask: { value: "1234567890123456.123456789014", asOf: QUOTE_TIME },
    });
    assert.equal(selectMarketReferencePrice(precise)?.value, "1234567890123456.123456789013");
  });
});
