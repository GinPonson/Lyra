import { Decimal } from "decimal.js";

import type { MarketQuote, MarketReferencePrice } from "./types.js";

const Price = Decimal.clone({ precision: 64 });

/** A single documented policy across adapters: valid midpoint, then trade, then bar close. */
export function selectMarketReferencePrice(quote: MarketQuote): MarketReferencePrice | null {
  const { bid, ask } = quote;
  if (bid !== null && ask !== null && bid.asOf === ask.asOf) {
    const buy = new Price(bid.value);
    const sell = new Price(ask.value);
    // 两个适配器都把供应商的 0 当作缺失；中间价必须同样要求买价为正，
    // 否则 bid=0、ask=1 会算出 0.5 这一半卖价，凭空造出可估值价格。
    if (sell.greaterThan(0) && buy.greaterThan(0) && sell.greaterThanOrEqualTo(buy)) {
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

/**
 * 同一快照时点的买价高于卖价即视为交叉盘口。
 *
 * 选价函数遇到交叉盘口只是静默回退到 last / close，无法与“本来就没有可用参考价”
 * 区分；调用方需要显式信号，才能把供应商数据形状错误（invalid_response）与单纯
 * 缺数据（no_data）分开。
 *
 * @param quote - 已通过形状校验的标准报价。
 * @returns 买卖价同时存在、时点相同且卖价低于买价时为 true。
 */
export function isCrossedMarket(quote: MarketQuote): boolean {
  const { bid, ask } = quote;
  if (bid === null || ask === null || bid.asOf !== ask.asOf) return false;
  return new Price(ask.value).lessThan(new Price(bid.value));
}
