import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createMarketDataRuntimeFromEnvironment, createMarketDataServiceFromEnvironment, marketDataEnvironmentSchema } from "../src/infrastructure/market-data/market-data-runtime.js";

describe("market data composition configuration", () => {
  it("defaults to disabled without credentials or network calls", async () => {
    let calls = 0;
    const data = createMarketDataServiceFromEnvironment({}, {
      fetchFn: async () => { calls += 1; throw new Error("Network is not allowed during configuration"); },
    });
    assert.equal(data.status().enabled, false);
    assert.equal((await data.getQuote({ assetType: "stock", market: "US", symbol: "AMD", currency: "USD" })).issue, "not_configured");
    assert.equal(calls, 0);
  });

  it("selects independent ordered routes and keeps credentials out of public status", () => {
    const data = createMarketDataServiceFromEnvironment({
      LYRA_MARKET_STOCK_PROVIDERS: "alpaca,marketdata",
      LYRA_MARKET_OPTION_PROVIDERS: "marketdata",
      LYRA_ALPACA_API_KEY: "private-alpaca-key",
      LYRA_ALPACA_SECRET_KEY: "private-alpaca-secret",
      LYRA_MARKETDATA_API_TOKEN: "private-market-token",
    });
    const status = data.status();
    assert.equal(status.enabled, true);
    assert.deepEqual(status.routes, { stock: ["alpaca", "marketdata"], option: ["marketdata"] });
    assert.deepEqual(status.providers.map((entry) => [entry.id, entry.cacheTtlSeconds]), [["alpaca", 900], ["marketdata", 86_400]]);
    assert.doesNotMatch(JSON.stringify(status), /private-|secret|token|apiKey/);
  });

  it("rejects unknown/duplicate routes, missing keys and partial Alpaca credentials", () => {
    for (const environment of [
      { LYRA_MARKET_STOCK_PROVIDERS: "unknown" },
      { LYRA_MARKET_OPTION_PROVIDERS: "alpaca,alpaca" },
      { LYRA_MARKET_STOCK_PROVIDERS: "none,alpaca" },
      { LYRA_MARKET_STOCK_PROVIDERS: "alpaca" },
      { LYRA_MARKET_OPTION_PROVIDERS: "marketdata" },
      { LYRA_ALPACA_API_KEY: "partial" },
      { LYRA_MARKET_TIMEOUT_MS: "0" },
      { LYRA_MARKETDATA_DAILY_REQUEST_LIMIT: "-1" },
      { LYRA_MARKETDATA_MODE: "real-time" },
    ]) assert.equal(marketDataEnvironmentSchema.safeParse(environment).success, false);
  });

  it("registers unused credentials for UI selection without enabling reads and changes cache policy explicitly", () => {
    const unused = createMarketDataServiceFromEnvironment({ LYRA_MARKETDATA_API_TOKEN: "private-token" });
    assert.equal(unused.status().enabled, false);
    assert.deepEqual(unused.status().providers.map((entry) => entry.id), ["marketdata"]);
    const delayed = createMarketDataServiceFromEnvironment({
      LYRA_MARKET_OPTION_PROVIDERS: "marketdata", LYRA_MARKETDATA_API_TOKEN: "private-token",
      LYRA_MARKETDATA_MODE: "delayed",
    });
    assert.equal(delayed.status().providers[0]?.cacheTtlSeconds, 900);
  });

  it("exposes a secret-free selectable catalog and can enable a configured source without rebuilding the runtime", async () => {
    let calls = 0;
    const runtime = createMarketDataRuntimeFromEnvironment({ LYRA_MARKETDATA_API_TOKEN: "private-token" }, {
      fetchFn: async () => { calls += 1; return new Response(null, { status: 503 }); },
    });
    assert.deepEqual(runtime.defaultRoutes, { stock: [], option: [] });
    assert.deepEqual(runtime.providers.map((entry) => [entry.id, entry.configured]), [["alpaca", false], ["marketdata", true]]);
    assert.doesNotMatch(JSON.stringify(runtime.providers), /private-token|apiToken|apiKey|secret/u);
    const instrument = { assetType: "stock", market: "US", symbol: "AMD", currency: "USD" } as const;
    await runtime.reader.getQuote(instrument);
    assert.equal(calls, 0);
    runtime.reader.setRoutes({ stock: ["marketdata"], option: [] }, 1);
    await runtime.reader.getQuote(instrument);
    assert.equal(calls, 1);
    assert.equal(runtime.reader.status().settingsRevision, 1);
    assert.throws(() => runtime.reader.setRoutes({ stock: ["alpaca"], option: [] }, 2));
    assert.deepEqual(runtime.reader.status().routes.stock, ["marketdata"]);
  });
});
