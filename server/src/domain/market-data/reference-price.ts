import { Decimal } from "decimal.js";

import type { MarketQuote, MarketReferencePrice } from "./types.js";

const Price = Decimal.clone({ precision: 64 });

/** A single documented policy across adapters: valid midpoint, then trade, then bar close. */
export function selectMarketReferencePrice(quote: MarketQuote): MarketReferencePrice | null {
  const { bid, ask } = quote;
  if (bid !== null && ask !== null && bid.asOf === ask.asOf) {
    const buy = new Price(bid.value);
    const sell = new Price(ask.value);
    if (sell.greaterThan(0) && buy.greaterThanOrEqualTo(0) && sell.greaterThanOrEqualTo(buy)) {
      return { value: buy.plus(sell).dividedBy(2).toFixed(), asOf: bid.asOf, basis: "mid" };
    }
  }
  for (const basis of ["last", "close"] as const) {
    const point = quote[basis];
    if (point !== null && new Price(point.value).greaterThan(0)) {
      return { ...point, basis };
    }
  }
  return null;
}
