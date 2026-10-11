import type { MarketDataProvider, MarketInstrument, MarketQuote, ProviderQuoteResult } from "../../domain/market-data/types.js";
import { requestMarketJson } from "./http.js";
import type { MarketDataFetch } from "./http.js";
import { isRecord, pricePoint, providerInstrument, unixMarketTimestamp, validCredential } from "./identifiers.js";

export interface MarketDataAppProviderOptions {
  apiToken: string;
  fetchFn?: MarketDataFetch;
  /** Deployment entitlement, not an inference from successful HTTP or snapshot age. */
  timing?: MarketQuote["timing"];
}

/** Single-symbol latest-available quotes; account entitlements determine the source snapshot. */
export class MarketDataAppProvider implements MarketDataProvider {
  readonly id = "marketdata";
  readonly assetTypes = ["stock", "option"] as const;
  #apiToken: string;
  #fetchFn: MarketDataFetch;
  #timing: MarketQuote["timing"];

  constructor(options: MarketDataAppProviderOptions) {
    this.#apiToken = options.apiToken.trim();
    this.#fetchFn = options.fetchFn ?? globalThis.fetch;
    const timing = options.timing ?? { kind: "end_of_day", delayMinutes: null };
    if ((timing.kind === "end_of_day" && timing.delayMinutes !== null)
      || (timing.kind === "delayed" && (!Number.isFinite(timing.delayMinutes)
        || timing.delayMinutes === null || timing.delayMinutes <= 0))
      || (timing.kind !== "delayed" && timing.kind !== "end_of_day")) {
      throw new RangeError("MarketData timing must describe delayed or end-of-day data");
    }
    this.#timing = { ...timing };
  }

  async fetchQuote(
    instrument: MarketInstrument,
    context: { now: Date; signal: AbortSignal },
  ): Promise<ProviderQuoteResult> {
    const identity = providerInstrument(instrument);
    if (!identity.ok) return { status: "unavailable", reason: identity.reason };
    if (!validCredential(this.#apiToken)) return { status: "unavailable", reason: "not_configured" };
    const now = context.now.getTime();
    if (!Number.isFinite(now)) return { status: "unavailable", reason: "invalid_response" };
    const resource = instrument.assetType === "stock" ? "stocks" : "options";
    const url = new URL(`https://api.marketdata.app/v1/${resource}/quotes/${encodeURIComponent(identity.symbol)}/`);
    // Paid accounts otherwise default to live: request the same freshness we report.
    url.searchParams.set("mode", this.#timing.kind === "end_of_day" ? "historical" : "delayed");
    // Do not add date/from/to: this contract is latest_available, not a historical query.
    const response = await requestMarketJson(url, {
      authorization: `Bearer ${this.#apiToken}`,
    }, context.signal, this.#fetchFn);
    if (!response.ok) return { status: "unavailable", reason: response.reason };
    const payload = response.payload;
    if (isRecord(payload) && payload.s === "no_data") {
      return { status: "unavailable", reason: "no_data" };
    }
    if (response.status === 404) return { status: "unavailable", reason: "provider_unavailable" };
    try {
      if (!isRecord(payload) || payload.s !== "ok") throw new Error("Invalid MarketData status");
      const symbolField = instrument.assetType === "stock" ? "symbol" : "optionSymbol";
      const symbols = payload[symbolField];
      if (!Array.isArray(symbols) || symbols.length !== 1 || symbols[0] !== identity.symbol) {
        throw new Error("MarketData symbol mismatch");
      }
      const asOf = unixMarketTimestamp(singleColumn(payload, "updated"), now);
      const bid = pricePoint(singleColumn(payload, "bid"), asOf);
      const ask = pricePoint(singleColumn(payload, "ask"), asOf);
      const last = pricePoint(singleColumn(payload, "last"), asOf);
      if (bid === null && ask === null && last === null) return { status: "unavailable", reason: "no_data" };
      return {
        status: "available",
        quote: {
          instrumentKey: identity.instrumentKey,
          providerId: this.id,
          feed: `MarketData ${resource} quotes (account-entitled snapshot)`,
          currency: "USD",
          bid,
          ask,
          last,
          close: null,
          timing: { ...this.#timing },
          fetchedAt: context.now.toISOString(),
        },
      };
    } catch {
      return { status: "unavailable", reason: "invalid_response" };
    }
  }
}

function singleColumn(payload: Record<string, unknown>, column: string): unknown {
  const values = payload[column];
  if (!Array.isArray(values) || values.length !== 1) throw new Error("Invalid MarketData column");
  return values[0];
}
