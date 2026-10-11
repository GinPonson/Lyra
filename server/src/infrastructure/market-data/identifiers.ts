import { Decimal } from "decimal.js";

import { marketInstrumentKey } from "../../domain/market-data/types.js";
import type { MarketInstrument, MarketPricePoint, MarketQuoteIssue } from "../../domain/market-data/types.js";

export type ProviderInstrument =
  | { ok: true; symbol: string; instrumentKey: string }
  | { ok: false; reason: MarketQuoteIssue };

/** Reject adjusted/nonstandard option roots rather than guessing a vendor alias. */
export function providerInstrument(instrument: MarketInstrument): ProviderInstrument {
  if (instrument.currency !== "USD") return { ok: false, reason: "currency_unsupported" };
  if (instrument.market !== "US") return { ok: false, reason: "unsupported_instrument" };
  if (instrument.assetType === "stock") {
    if (typeof instrument.symbol !== "string"
      || !/^[A-Z][A-Z0-9]{0,9}(?:[.-][A-Z0-9]{1,2})?$/.test(instrument.symbol)) {
      return { ok: false, reason: "unsupported_instrument" };
    }
    return { ok: true, symbol: instrument.symbol, instrumentKey: marketInstrumentKey(instrument) };
  }
  if (instrument.assetType !== "option"
    || typeof instrument.underlying !== "string"
    || typeof instrument.expiry !== "string"
    || typeof instrument.strike !== "string"
    || !/^[A-Z]{1,6}$/.test(instrument.underlying)
    || !validCalendarDate(instrument.expiry)
    || !/^20\d{2}-/.test(instrument.expiry)
    || !/^(?:0|[1-9]\d*)(?:\.\d{1,3})?$/.test(instrument.strike)
    || (instrument.optionType !== "C" && instrument.optionType !== "P")
    || instrument.multiplier !== "100") {
    return { ok: false, reason: "unsupported_instrument" };
  }
  const [whole = "", fraction = ""] = instrument.strike.split(".");
  // Eight OCC strike digits represent thousandths of a dollar, not contract value.
  if (whole.length > 5) return { ok: false, reason: "unsupported_instrument" };
  const strikeThousandths = BigInt(whole) * 1000n + BigInt(fraction.padEnd(3, "0"));
  if (strikeThousandths <= 0n || strikeThousandths > 99_999_999n) {
    return { ok: false, reason: "unsupported_instrument" };
  }
  const expiry = instrument.expiry.slice(2).replaceAll("-", "");
  return {
    ok: true,
    symbol: `${instrument.underlying}${expiry}${instrument.optionType}${strikeThousandths.toString().padStart(8, "0")}`,
    instrumentKey: marketInstrumentKey(instrument),
  };
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Providers document JSON numbers. Do not coerce text, booleans, or absent fields. */
export function pricePoint(value: unknown, asOf: string): MarketPricePoint | null {
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value)
    || value < 0 || value > Number.MAX_SAFE_INTEGER) {
    throw new Error("Invalid market price");
  }
  // A zero bid/ask is an absent executable price, not a free security.
  return value === 0 ? null : { value: new Decimal(value).toFixed(), asOf };
}

/** Preserve Alpaca's nanosecond timestamp text after checking the requested window. */
export function marketTimestamp(value: unknown, earliest: number, latest: number): string {
  if (typeof value !== "string"
    || !/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.test(value)
    || !validCalendarDate(value.slice(0, 10))) {
    throw new Error("Invalid market timestamp");
  }
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds) || milliseconds < earliest || milliseconds > latest) {
    throw new Error("Market timestamp outside requested window");
  }
  return value;
}

/** MarketData's updated values are Unix seconds, not JS milliseconds. */
export function unixMarketTimestamp(value: unknown, now: number): string {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0
    || value * 1000 > now) {
    throw new Error("Invalid Unix market timestamp");
  }
  return new Date(value * 1000).toISOString();
}

export function validCredential(value: string): boolean {
  return /^[\x21-\x7e]+$/.test(value);
}

function validCalendarDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}
