/** Provider-neutral identity. US standard option contracts are the first supported market. */
export type MarketInstrument =
  | { assetType: "stock"; market: "US"; symbol: string; currency: "USD" }
  | {
      assetType: "option";
      market: "US";
      underlying: string;
      expiry: string;
      strike: string;
      optionType: "C" | "P";
      multiplier: "100";
      currency: "USD";
    };

export type MarketAssetType = MarketInstrument["assetType"];

/**
 * Decimal prices with the source snapshot time, or interval end for a bar close.
 * A snapshot timestamp is not necessarily the time of the last individual trade.
 */
export interface MarketPricePoint {
  value: string;
  asOf: string;
}

export interface MarketQuote {
  instrumentKey: string;
  providerId: string;
  /** Human-readable feed provenance; not interpreted by application calculations. */
  feed: string;
  currency: "USD";
  bid: MarketPricePoint | null;
  ask: MarketPricePoint | null;
  last: MarketPricePoint | null;
  close: MarketPricePoint | null;
  timing: { kind: "delayed" | "end_of_day"; delayMinutes: number | null };
  fetchedAt: string;
}

export const MARKET_QUOTE_ISSUES = [
  "not_configured", "unsupported_instrument", "currency_unsupported", "no_data",
  "rate_limited", "credentials_rejected", "provider_unavailable", "invalid_response", "timeout",
] as const;
export type MarketQuoteIssue = typeof MARKET_QUOTE_ISSUES[number];

export type ProviderQuoteResult =
  | { status: "available"; quote: MarketQuote }
  | { status: "unavailable"; reason: MarketQuoteIssue };

/** Each adapter translates identifiers, timestamps, prices and errors at this boundary. */
export interface MarketDataProvider {
  readonly id: string;
  readonly assetTypes: readonly MarketAssetType[];
  fetchQuote(
    instrument: MarketInstrument,
    context: { now: Date; signal: AbortSignal },
  ): Promise<ProviderQuoteResult>;
}

/** Selected by Lyra, never by a vendor's opaque mark calculation. */
export interface MarketReferencePrice extends MarketPricePoint {
  basis: "mid" | "last" | "close";
}

export interface MarketQuoteLookup {
  status: "available" | "stale" | "unavailable";
  quote: MarketQuote | null;
  referencePrice: MarketReferencePrice | null;
  issue: MarketQuoteIssue | null;
  cached: boolean;
}

export interface MarketDataReader {
  getQuote(instrument: MarketInstrument): Promise<MarketQuoteLookup>;
  status(): MarketDataStatus;
  /** Mutable readers pin routing for a whole valuation, including queued symbol reads. */
  snapshot?(): MarketDataReader;
}

export interface MarketDataStatus {
  enabled: boolean;
  /** Revision of the route settings used by this read; omitted by legacy static readers. */
  settingsRevision?: number;
  routes: Readonly<Record<MarketAssetType, readonly string[]>>;
  providers: readonly {
    id: string;
    assetTypes: readonly MarketAssetType[];
    cacheTtlSeconds: number;
  }[];
}

/** Stable canonical key: vendor OCC prefixes and stock aliases never become domain IDs. */
export function marketInstrumentKey(instrument: MarketInstrument): string {
  return instrument.assetType === "stock"
    ? `US:stock:${instrument.symbol}`
    : `US:option:${instrument.underlying}:${instrument.expiry}:${instrument.optionType}:${instrument.strike}`;
}
