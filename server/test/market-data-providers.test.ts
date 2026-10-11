import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { marketInstrumentKey } from "../src/domain/market-data/types.js";
import type { MarketInstrument, MarketQuote, ProviderQuoteResult } from "../src/domain/market-data/types.js";
import { AlpacaMarketDataProvider } from "../src/infrastructure/market-data/alpaca-provider.js";
import { MarketDataAppProvider } from "../src/infrastructure/market-data/marketdata-provider.js";
import { MARKET_DATA_MAX_BODY_BYTES, MARKET_DATA_TIMEOUT_MILLISECONDS } from "../src/infrastructure/market-data/http.js";
import type { MarketDataFetch } from "../src/infrastructure/market-data/http.js";

const now = new Date("2026-10-09T20:00:00.000Z");
const stock: MarketInstrument = { assetType: "stock", market: "US", symbol: "AAPL", currency: "USD" };
const option: MarketInstrument = {
  assetType: "option", market: "US", underlying: "AAPL", expiry: "2026-10-16",
  strike: "250.125", optionType: "C", multiplier: "100", currency: "USD",
};
const optionSymbol = "AAPL261016C00250125";

function context(signal: AbortSignal = new AbortController().signal): { now: Date; signal: AbortSignal } {
  return { now, signal };
}

function json(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });
}

/** Alpaca REST row shape: sizes are deliberately present to catch accidental unit conversion. */
function alpacaStockFixture(): Record<string, unknown> {
  return {
    symbol: "AAPL", next_page_token: null,
    quotes: [{
      t: "2026-10-09T19:43:55.123456789Z", bp: 250.125, ap: 250.175,
      bs: 2, as: 4, bx: "Q", ax: "Q", c: ["R"], z: "C",
    }],
  };
}

function alpacaOptionFixture(): Record<string, unknown> {
  return {
    bars: { [optionSymbol]: [{
      t: "2026-10-09T19:43:00Z", o: 12.3, h: 12.5, l: 12.2,
      c: 12.34, v: 25, n: 4, vw: 12.35,
    }] },
    next_page_token: null,
  };
}

/** MarketData returns column arrays and Unix seconds, not an array of quote objects. */
function marketDataFixture(assetType: "stock" | "option" = "stock"): Record<string, unknown> {
  return {
    s: "ok",
    [assetType === "stock" ? "symbol" : "optionSymbol"]: [assetType === "stock" ? "AAPL" : optionSymbol],
    bid: [5.15], ask: [5.25], last: [5.2], mid: [123456],
    bidSize: [994], askSize: [57], volume: [977],
    updated: [Date.parse("2026-10-08T20:00:00Z") / 1000],
    iv: [null], delta: [null], gamma: [null], theta: [null], vega: [null],
  };
}

function quote(result: ProviderQuoteResult): MarketQuote {
  assert.equal(result.status, "available");
  if (result.status !== "available") throw new Error("Expected fixture quote");
  return result.quote;
}

function alpaca(fetchFn: MarketDataFetch): AlpacaMarketDataProvider {
  return new AlpacaMarketDataProvider({ apiKey: "test-key", secretKey: "test-secret", fetchFn });
}

function marketData(fetchFn: MarketDataFetch): MarketDataAppProvider {
  return new MarketDataAppProvider({ apiToken: "test-token", fetchFn });
}

describe("Alpaca market data adapter", () => {
  it("requests one historical SIP quote with a 16-minute cutoff and preserves decimal prices/timestamps", async () => {
    const calls: { url: URL; init: RequestInit | undefined }[] = [];
    const provider = alpaca(async (input, init) => {
      calls.push({ url: new URL(input.toString()), init });
      return json(alpacaStockFixture());
    });
    const result = quote(await provider.fetchQuote(stock, context()));
    assert.equal(calls.length, 1);
    const call = calls[0]!;
    assert.equal(call.url.origin, "https://data.alpaca.markets");
    assert.equal(call.url.pathname, "/v2/stocks/AAPL/quotes");
    assert.equal(call.url.searchParams.get("feed"), "sip");
    assert.equal(call.url.searchParams.get("end"), "2026-10-09T19:44:00.000Z");
    assert.equal(call.url.searchParams.get("start"), "2026-10-02T19:44:00.000Z");
    assert.equal(call.url.searchParams.get("sort"), "desc");
    assert.equal(call.url.searchParams.get("limit"), "1");
    assert.equal(call.init?.redirect, "error");
    const headers = new Headers(call.init?.headers);
    assert.equal(headers.get("APCA-API-KEY-ID"), "test-key");
    assert.equal(headers.get("APCA-API-SECRET-KEY"), "test-secret");
    assert.equal(call.url.toString().includes("test-secret"), false);
    assert.equal(result.instrumentKey, marketInstrumentKey(stock));
    assert.deepEqual(result.bid, { value: "250.125", asOf: "2026-10-09T19:43:55.123456789Z" });
    assert.equal(result.ask?.value, "250.175");
    assert.equal(result.last, null);
    assert.equal(result.close, null);
    assert.equal("bidSize" in result, false);
    assert.deepEqual(result.timing, { kind: "delayed", delayMinutes: 16 });
    assert.equal(result.fetchedAt, now.toISOString());
  });

  it("encodes standard OCC thousandths and returns an OPRA minute close without fabricating bid/ask", async () => {
    let requested: URL | undefined;
    const provider = alpaca(async (input) => {
      requested = new URL(input.toString());
      return json(alpacaOptionFixture());
    });
    const result = quote(await provider.fetchQuote(option, context()));
    assert.equal(requested?.pathname, "/v1beta1/options/bars");
    assert.equal(requested?.searchParams.get("symbols"), optionSymbol);
    assert.equal(requested?.searchParams.get("timeframe"), "1Min");
    assert.equal(requested?.searchParams.has("feed"), false);
    assert.equal(requested?.searchParams.get("limit"), "1");
    assert.equal(requested?.searchParams.get("end"), "2026-10-09T19:43:59.999Z");
    assert.deepEqual(result.close, { value: "12.34", asOf: "2026-10-09T19:44:00.000Z" });
    assert.ok(result.close !== null);
    assert.ok(Date.parse(result.close.asOf) <= now.getTime() - 16 * 60_000);
    assert.equal(result.bid, null);
    assert.equal(result.ask, null);
    assert.equal(result.last, null);
    assert.match(result.feed, /OPRA.*bar close.*bar end timestamp/);
    assert.equal(result.instrumentKey, marketInstrumentKey(option));
  });

  it("accepts genuine empty quote/bar results without a fictitious price", async () => {
    for (const quotes of [null, []]) {
      assert.deepEqual(await alpaca(async () => json({ symbol: "AAPL", quotes })).fetchQuote(stock, context()),
        { status: "unavailable", reason: "no_data" });
    }
    for (const bars of [null, {}, { [optionSymbol]: [] }, { [optionSymbol]: null }]) {
      assert.deepEqual(await alpaca(async () => json({ bars })).fetchQuote(option, context()),
        { status: "unavailable", reason: "no_data" });
    }
    assert.deepEqual(await alpaca(async () => json({ bars: { [optionSymbol]: [{
      t: "2026-10-09T19:43:00Z", c: 12.34, v: 0,
    }] } })).fetchQuote(option, context()), { status: "unavailable", reason: "no_data" });
  });

  it("rejects mismatched symbols, broken shapes, bad prices, and quotes outside the requested window", async () => {
    const fixtures: unknown[] = [
      { ...alpacaStockFixture(), symbol: "MSFT" },
      { symbol: "AAPL" },
      { symbol: "AAPL", quotes: [{ t: "2026-10-09T19:43:00Z", bp: "250", ap: 251 }] },
      { symbol: "AAPL", quotes: [{ t: "2026-10-09T19:43:00Z", bp: -1, ap: 251 }] },
      { symbol: "AAPL", quotes: [{ t: "2026-10-09T19:43:00Z", ap: 251 }] },
      { symbol: "AAPL", quotes: [{ t: "2026-10-09T19:44:01Z", bp: 250, ap: 251 }] },
      { symbol: "AAPL", quotes: [{ t: "2026-10-01T19:44:00Z", bp: 250, ap: 251 }] },
      { symbol: "AAPL", quotes: [{ t: "2026-02-30T19:44:00Z", bp: 250, ap: 251 }] },
      { symbol: "AAPL", quotes: [null] },
    ];
    for (const fixture of fixtures) {
      assert.deepEqual(await alpaca(async () => json(fixture)).fetchQuote(stock, context()),
        { status: "unavailable", reason: "invalid_response" });
    }
    for (const bars of [
      { MSFT261016C00250125: [{ t: "2026-10-09T19:43:00Z", c: 12.34, v: 25 }] },
      { [optionSymbol]: [{ t: "2026-10-09T19:44:00Z", c: 12.34, v: 25 }] },
      { [optionSymbol]: [{ t: "2026-10-09T19:43:30Z", c: 12.34, v: 25 }] },
      { [optionSymbol]: [{ t: "2026-10-09T19:43:00Z", c: 12.34, v: -1 }] },
    ]) {
      assert.deepEqual(await alpaca(async () => json({ bars })).fetchQuote(option, context()),
        { status: "unavailable", reason: "invalid_response" });
    }
  });
});

describe("MarketData market data adapter", () => {
  it("accepts cached HTTP 203, decodes columns and Unix seconds, and keeps the default EOD timing", async () => {
    let requested: URL | undefined;
    let options: RequestInit | undefined;
    const provider = marketData(async (input, init) => {
      requested = new URL(input.toString());
      options = init;
      return json(marketDataFixture(), 203);
    });
    const result = quote(await provider.fetchQuote(stock, context()));
    assert.equal(requested?.toString(), "https://api.marketdata.app/v1/stocks/quotes/AAPL/?mode=historical");
    assert.equal(new Headers(options?.headers).get("authorization"), "Bearer test-token");
    assert.equal(options?.redirect, "error");
    assert.deepEqual(result.bid, { value: "5.15", asOf: "2026-10-08T20:00:00.000Z" });
    assert.equal(result.ask?.value, "5.25");
    assert.equal(result.last?.value, "5.2");
    assert.equal(result.close, null);
    assert.equal("mid" in result, false);
    assert.equal("bidSize" in result, false);
    assert.deepEqual(result.timing, { kind: "end_of_day", delayMinutes: null });
  });

  it("requests exactly one option and does not multiply a premium by the contract multiplier", async () => {
    let requested: URL | undefined;
    const result = quote(await marketData(async (input) => {
      requested = new URL(input.toString());
      return json(marketDataFixture("option"));
    }).fetchQuote(option, context()));
    assert.equal(requested?.pathname, `/v1/options/quotes/${optionSymbol}/`);
    assert.equal(requested?.search, "?mode=historical");
    assert.equal(result.bid?.value, "5.15");
    assert.equal(result.last?.value, "5.2");
    assert.equal(result.instrumentKey, marketInstrumentKey(option));
    assert.equal("delta" in result, false);
  });

  it("uses explicit deployment timing without upgrading a current snapshot to real-time", async () => {
    const timing: MarketQuote["timing"] = { kind: "delayed", delayMinutes: 15 };
    const urls: URL[] = [];
    const provider = new MarketDataAppProvider({ apiToken: "token", timing, fetchFn: async (input) => {
      const url = new URL(input.toString());
      urls.push(url);
      return json({
        ...marketDataFixture(url.pathname.includes("/options/") ? "option" : "stock"),
        updated: [(now.getTime() - 15 * 60_000) / 1000],
      });
    } });
    timing.delayMinutes = 1;
    const result = quote(await provider.fetchQuote(stock, context()));
    assert.deepEqual(result.timing, { kind: "delayed", delayMinutes: 15 });
    assert.equal(result.bid?.asOf, "2026-10-09T19:45:00.000Z");
    quote(await provider.fetchQuote(option, context()));
    assert.deepEqual(urls.map((url) => url.searchParams.get("mode")), ["delayed", "delayed"]);
    assert.deepEqual(urls.map((url) => url.pathname), [
      "/v1/stocks/quotes/AAPL/", `/v1/options/quotes/${optionSymbol}/`,
    ]);
    assert.throws(() => new MarketDataAppProvider({
      apiToken: "token", timing: { kind: "delayed", delayMinutes: 0 },
    }), RangeError);
    assert.throws(() => new MarketDataAppProvider({
      apiToken: "token", timing: { kind: "end_of_day", delayMinutes: 15 },
    }), RangeError);
  });

  it("does not turn absent prices or vendor no_data into zero-price valuations", async () => {
    for (const status of [200, 203, 404]) {
      assert.deepEqual(await marketData(async () => json({ s: "no_data", errmsg: "no quotes" }, status))
        .fetchQuote(option, context()), { status: "unavailable", reason: "no_data" });
    }
    assert.deepEqual(await marketData(async () => json({
      ...marketDataFixture(), bid: [0], ask: [null], last: [0],
    })).fetchQuote(stock, context()), { status: "unavailable", reason: "no_data" });
    const oneSide = quote(await marketData(async () => json({
      ...marketDataFixture(), bid: [0], last: [null],
    })).fetchQuote(stock, context()));
    assert.equal(oneSide.bid, null);
    assert.equal(oneSide.last, null);
    assert.equal(oneSide.ask?.value, "5.25");
  });

  it("rejects wrong symbols, missing/misaligned columns, text prices, and milliseconds in a Unix-seconds column", async () => {
    for (const changes of [
      { symbol: ["MSFT"] }, { symbol: ["AAPL", "AAPL"] }, { bid: [] }, { ask: [5.25, 6] },
      { bid: ["5.15"] }, { ask: [-1] }, { last: [Number.MAX_VALUE] }, { last: undefined },
      { updated: [now.getTime()] }, { updated: [now.getTime() / 1000 + 1] },
      { updated: ["1791568800"] }, { s: "error", errmsg: "test-token secret" },
    ]) {
      assert.deepEqual(await marketData(async () => json({ ...marketDataFixture(), ...changes }))
        .fetchQuote(stock, context()), { status: "unavailable", reason: "invalid_response" });
    }
    assert.deepEqual(await marketData(async () => json({
      ...marketDataFixture("option"), optionSymbol: ["AAPL261016P00250125"],
    })).fetchQuote(option, context()), { status: "unavailable", reason: "invalid_response" });
  });
});

describe("Market data identifier and HTTP boundary", () => {
  it("rejects unsafe symbols, nonstandard contracts, bad dates and unrepresentable strikes before HTTP", async () => {
    let calls = 0;
    const fetchFn: MarketDataFetch = async () => { calls += 1; return json({}); };
    const unsupported: MarketInstrument[] = [
      { ...stock, symbol: "aapl" }, { ...stock, symbol: "AAPL/../MSFT" },
      { ...option, underlying: "AAPL1" }, { ...option, underlying: "BRK.B" },
      { ...option, underlying: "TOOLONG" }, { ...option, expiry: "2026-02-30" },
      { ...option, expiry: "2100-01-01" }, { ...option, strike: "1.0001" },
      { ...option, strike: "1e2" }, { ...option, strike: "100000" },
      { ...option, strike: "0" }, { ...option, strike: "-1" },
      { ...option, multiplier: "10" } as unknown as MarketInstrument,
    ];
    for (const provider of [alpaca(fetchFn), marketData(fetchFn)]) {
      for (const instrument of unsupported) {
        assert.deepEqual(await provider.fetchQuote(instrument, context()),
          { status: "unavailable", reason: "unsupported_instrument" });
      }
      assert.deepEqual(await provider.fetchQuote({ ...stock, currency: "EUR" } as unknown as MarketInstrument, context()),
        { status: "unavailable", reason: "currency_unsupported" });
    }
    assert.equal(calls, 0);
  });

  it("encodes puts, low strikes and the maximum OCC strike without binary rounding", async () => {
    const cases = [["0.001", "00000001"], ["0.01", "00000010"], ["1.005", "00001005"], ["99999.999", "99999999"]] as const;
    for (const [strike, digits] of cases) {
      let requested = "";
      const provider = marketData(async (input) => { requested = input.toString(); return json({ s: "no_data" }, 404); });
      await provider.fetchQuote({ ...option, optionType: "P", strike }, context());
      assert.equal(new URL(requested).pathname, `/v1/options/quotes/AAPL261016P${digits}/`);
    }
  });

  it("does not request data with missing credentials and does not serialize provider secrets", async () => {
    let calls = 0;
    const fetchFn: MarketDataFetch = async () => { calls += 1; return json({}); };
    for (const provider of [
      new AlpacaMarketDataProvider({ apiKey: "", secretKey: "hidden-secret", fetchFn }),
      new MarketDataAppProvider({ apiToken: "   ", fetchFn }),
      new MarketDataAppProvider({ apiToken: "line\nbreak", fetchFn }),
    ]) {
      assert.deepEqual(await provider.fetchQuote(stock, context()), { status: "unavailable", reason: "not_configured" });
      assert.equal(JSON.stringify(provider).includes("hidden-secret"), false);
    }
    assert.equal(JSON.stringify(alpaca(fetchFn)).includes("test-secret"), false);
    assert.equal(JSON.stringify(marketData(fetchFn)).includes("test-token"), false);
    assert.equal(calls, 0);
  });

  it("maps HTTP authentication, throttling, redirects and failures without leaking response bodies", async () => {
    const cases = [[401, "credentials_rejected"], [403, "credentials_rejected"], [429, "rate_limited"],
      [500, "provider_unavailable"], [302, "provider_unavailable"], [404, "provider_unavailable"]] as const;
    for (const create of [alpaca, marketData]) {
      for (const [status, reason] of cases) {
        let calls = 0;
        const provider = create(async (_input, init) => {
          calls += 1;
          assert.equal(init?.redirect, "error");
          return json({ s: "error", errmsg: "test-secret test-token raw account detail" }, status);
        });
        const result: ProviderQuoteResult = await provider.fetchQuote(stock, context());
        assert.deepEqual(result, { status: "unavailable", reason });
        assert.equal(calls, 1);
        assert.equal(JSON.stringify(result).includes("test-secret"), false);
      }
      assert.deepEqual(await create(async () => { throw new Error("test-secret test-token private URL"); })
        .fetchQuote(stock, context()), { status: "unavailable", reason: "provider_unavailable" });
    }
  });

  it("rejects malformed and oversized bodies, including streamed bodies without Content-Length", async () => {
    assert.deepEqual(await alpaca(async () => new Response("<html>private error</html>"))
      .fetchQuote(stock, context()), { status: "unavailable", reason: "invalid_response" });
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new Uint8Array(MARKET_DATA_MAX_BODY_BYTES + 1)); },
      cancel() { cancelled = true; },
    });
    assert.deepEqual(await marketData(async () => new Response(stream)).fetchQuote(stock, context()),
      { status: "unavailable", reason: "invalid_response" });
    assert.equal(cancelled, true);
    assert.deepEqual(await marketData(async () => new Response("{}", {
      headers: { "content-length": String(MARKET_DATA_MAX_BODY_BYTES + 1) },
    })).fetchQuote(stock, context()), { status: "unavailable", reason: "invalid_response" });
  });

  it("honors caller cancellation even if a transport ignores its signal", async () => {
    const controller = new AbortController();
    let signal: AbortSignal | null | undefined;
    const provider = alpaca(async (_input, init) => {
      signal = init?.signal;
      return new Promise<Response>(() => {});
    });
    const pending = provider.fetchQuote(stock, context(controller.signal));
    controller.abort(new Error("caller-private-reason"));
    assert.deepEqual(await pending, { status: "unavailable", reason: "timeout" });
    assert.equal(signal?.aborted, true);
    let calls = 0;
    assert.deepEqual(await marketData(async () => { calls += 1; return json({}); })
      .fetchQuote(stock, context(controller.signal)), { status: "unavailable", reason: "timeout" });
    assert.equal(calls, 0);
  });

  it("bounds a hanging request with its own deadline", async (test) => {
    test.mock.timers.enable({ apis: ["setTimeout"] });
    try {
      const pending = marketData(async () => new Promise<Response>(() => {})).fetchQuote(stock, context());
      test.mock.timers.tick(MARKET_DATA_TIMEOUT_MILLISECONDS + 1);
      assert.deepEqual(await pending, { status: "unavailable", reason: "timeout" });
    } finally {
      test.mock.timers.reset();
    }
  });

  it("keeps cancellation active while reading a stalled response body", async () => {
    const controller = new AbortController();
    let cancelled = false;
    let bodyStarted: () => void = () => {};
    const started = new Promise<void>((resolve) => { bodyStarted = resolve; });
    const stream = new ReadableStream<Uint8Array>({
      pull() { bodyStarted(); return new Promise<void>(() => {}); },
      cancel() { cancelled = true; },
    }, { highWaterMark: 0 });
    const pending = marketData(async () => new Response(stream)).fetchQuote(stock, context(controller.signal));
    await started;
    controller.abort();
    assert.deepEqual(await pending, { status: "unavailable", reason: "timeout" });
    assert.equal(cancelled, true);
  });

  it("releases a response when cancellation wins before fetch hands the body to the reader", async () => {
    const controller = new AbortController();
    let cancelled = false;
    let bodyStarted: () => void = () => {};
    const started = new Promise<void>((resolve) => { bodyStarted = resolve; });
    const stream = new ReadableStream<Uint8Array>({
      pull() { bodyStarted(); return new Promise<void>(() => {}); },
      cancel() { cancelled = true; },
    });
    const pending = marketData(async () => new Response(stream)).fetchQuote(stock, context(controller.signal));
    await started;
    controller.abort();
    assert.deepEqual(await pending, { status: "unavailable", reason: "timeout" });
    assert.equal(cancelled, true);
  });
});
