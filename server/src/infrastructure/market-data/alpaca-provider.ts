import type { MarketDataProvider, MarketInstrument, ProviderQuoteResult } from "../../domain/market-data/types.js";
import { requestMarketJson } from "./http.js";
import type { MarketDataFetch } from "./http.js";
import { isRecord, marketTimestamp, pricePoint, providerInstrument, validCredential } from "./identifiers.js";

const DELAY_MILLISECONDS = 16 * 60_000;
const LOOKBACK_MILLISECONDS = 7 * 24 * 60 * 60_000;

export interface AlpacaMarketDataProviderOptions {
  apiKey: string;
  secretKey: string;
  fetchFn?: MarketDataFetch;
}

/** Historical SIP quotes and genuine OPRA bar closes; never the indicative feed. */
export class AlpacaMarketDataProvider implements MarketDataProvider {
  readonly id = "alpaca";
  readonly assetTypes = ["stock", "option"] as const;
  #apiKey: string;
  #secretKey: string;
  #fetchFn: MarketDataFetch;

  constructor(options: AlpacaMarketDataProviderOptions) {
    this.#apiKey = options.apiKey.trim();
    this.#secretKey = options.secretKey.trim();
    this.#fetchFn = options.fetchFn ?? globalThis.fetch;
  }

  async fetchQuote(
    instrument: MarketInstrument,
    context: { now: Date; signal: AbortSignal },
  ): Promise<ProviderQuoteResult> {
    const identity = providerInstrument(instrument);
    if (!identity.ok) return { status: "unavailable", reason: identity.reason };
    if (!validCredential(this.#apiKey) || !validCredential(this.#secretKey)) {
      return { status: "unavailable", reason: "not_configured" };
    }
    const now = context.now.getTime();
    if (!Number.isFinite(now)) return { status: "unavailable", reason: "invalid_response" };
    const cutoff = now - DELAY_MILLISECONDS;
    const start = cutoff - LOOKBACK_MILLISECONDS;
    // Bar timestamps denote interval starts. Exclude a minute whose close can be after cutoff.
    const end = instrument.assetType === "option" ? Math.floor(cutoff / 60_000) * 60_000 - 1 : cutoff;
    const url = new URL(instrument.assetType === "stock"
      ? `https://data.alpaca.markets/v2/stocks/${encodeURIComponent(identity.symbol)}/quotes`
      : "https://data.alpaca.markets/v1beta1/options/bars");
    url.searchParams.set("start", new Date(start).toISOString());
    url.searchParams.set("end", new Date(end).toISOString());
    url.searchParams.set("sort", "desc");
    url.searchParams.set("limit", "1");
    if (instrument.assetType === "stock") url.searchParams.set("feed", "sip");
    else {
      url.searchParams.set("symbols", identity.symbol);
      url.searchParams.set("timeframe", "1Min");
    }
    const response = await requestMarketJson(url, {
      "APCA-API-KEY-ID": this.#apiKey,
      "APCA-API-SECRET-KEY": this.#secretKey,
    }, context.signal, this.#fetchFn);
    if (!response.ok) return { status: "unavailable", reason: response.reason };
    if (response.status === 404) return { status: "unavailable", reason: "provider_unavailable" };

    try {
      const payload = response.payload;
      if (!isRecord(payload)) throw new Error("Invalid Alpaca payload");
      const rows = instrument.assetType === "stock"
        ? stockRows(payload, identity.symbol)
        : optionRows(payload, identity.symbol);
      if (rows.length === 0) return { status: "unavailable", reason: "no_data" };
      if (rows.length !== 1 || !isRecord(rows[0])) throw new Error("Invalid Alpaca rows");
      const row = rows[0];
      const asOf = marketTimestamp(row.t, start, end);
      if (instrument.assetType === "option"
        && (Date.parse(asOf) % 60_000 !== 0 || Date.parse(asOf) + 60_000 > cutoff)) {
        throw new Error("Invalid Alpaca bar interval");
      }
      if (instrument.assetType === "option") {
        if (typeof row.v !== "number" || !Number.isSafeInteger(row.v) || row.v < 0) {
          throw new Error("Invalid Alpaca option volume");
        }
        if (row.v === 0) return { status: "unavailable", reason: "no_data" };
      }
      const bid = instrument.assetType === "stock" ? pricePoint(row.bp, asOf) : null;
      const ask = instrument.assetType === "stock" ? pricePoint(row.ap, asOf) : null;
      // The close belongs to the completed minute; this is its interval end, not a trade-event timestamp.
      const close = instrument.assetType === "option"
        ? pricePoint(row.c, new Date(Date.parse(asOf) + 60_000).toISOString())
        : null;
      if (bid === null && ask === null && close === null) {
        return { status: "unavailable", reason: "no_data" };
      }
      return {
        status: "available",
        quote: {
          instrumentKey: identity.instrumentKey,
          providerId: this.id,
          feed: instrument.assetType === "stock"
            ? "Alpaca SIP historical quotes"
            : "Alpaca OPRA historical 1-minute bar close (bar end timestamp)",
          currency: "USD",
          bid,
          ask,
          last: null,
          close,
          timing: { kind: "delayed", delayMinutes: 16 },
          fetchedAt: context.now.toISOString(),
        },
      };
    } catch {
      return { status: "unavailable", reason: "invalid_response" };
    }
  }
}

function stockRows(payload: Record<string, unknown>, symbol: string): unknown[] {
  if (payload.symbol !== symbol) throw new Error("Alpaca stock symbol mismatch");
  if (payload.quotes === null) return [];
  if (!Array.isArray(payload.quotes)) throw new Error("Invalid Alpaca quotes");
  return payload.quotes;
}

function optionRows(payload: Record<string, unknown>, symbol: string): unknown[] {
  if (payload.bars === null) return [];
  if (!isRecord(payload.bars)) throw new Error("Invalid Alpaca bars");
  const symbols = Object.keys(payload.bars);
  if (symbols.length === 0) return [];
  if (symbols.length !== 1 || symbols[0] !== symbol) throw new Error("Alpaca option symbol mismatch");
  const rows = payload.bars[symbol];
  if (rows === null) return [];
  if (!Array.isArray(rows)) throw new Error("Invalid Alpaca option rows");
  return rows;
}
