import { Decimal } from "decimal.js";

import { parseIsoDate } from "../domain/analytics/dates.js";
import type { PositionRow } from "../domain/analytics/types.js";
import { parseOptionSymbol } from "../domain/ledger/parsers.js";
import {
  marketInstrumentKey,
  type MarketDataReader,
  type MarketDataStatus,
  type MarketInstrument,
  type MarketQuoteIssue,
  type MarketQuoteLookup,
} from "../domain/market-data/types.js";
import type { ResolvedAccountScope } from "./account-scope.js";
import { createPublicPositionList, type PublicPosition } from "./ledger-detail-view.js";

const Money = Decimal.clone({ precision: 64 });
const MAX_CONCURRENT_QUOTES = 4;
const STOCK_SYMBOL = /^[A-Z]{1,6}(?:[.-][A-Z]{1,2})?$/u;
const STANDARD_OPTION_ROOT = /^[A-Z]{1,6}$/u;

export interface MarketPosition extends PublicPosition {
  currency: string;
  instrumentKey: string | null;
  quote: MarketQuoteLookup;
  valuation: {
    marketValue: string | null;
    unrealizedPnl: string | null;
    multiplier: string | null;
    basis: "reference_price_excluding_fees";
  };
}

export interface MarketPositionCurrencyGroup {
  currency: string;
  accountIds: readonly string[];
  positions: number;
  /** 包含有旧参考价的 stale 行；完整性还必须检查 complete。 */
  pricedPositions: number;
  complete: boolean;
  marketValue: string | null;
  unrealizedPnl: string | null;
  asOfFrom: string | null;
  asOfTo: string | null;
}

export interface MarketPositionView {
  rows: readonly MarketPosition[];
  count: number;
  /** 可给出行级参考估值的数量，包括 stale 行。 */
  pricedCount: number;
  complete: boolean;
  basis: "current_positions_at_quote_times_excluding_fees";
  marketData: MarketDataStatus;
  currencyGroups: readonly MarketPositionCurrencyGroup[];
}

/**
 * 为当前未平持仓附加供应商无关的参考估值，不改变成本、FIFO、现金或已实现损益。
 * 参考价格由 MarketDataReader 统一选择；这里仅处理身份、授权范围和金额口径。
 */
export async function createMarketPositionView(
  positions: readonly PositionRow[],
  scope: ResolvedAccountScope,
  reader: MarketDataReader,
): Promise<MarketPositionView> {
  // 在任何外部读取前一次性验证所有账户，禁止前几行已发请求后才发现越界行。
  const publicPositions = createPublicPositionList(positions, scope);
  const currencies = accountCurrencies(scope);
  const prepared = publicPositions.map((position, index) => {
    const source = positions[index];
    const currency = currencies.get(position.accountId);
    if (source === undefined || currency === undefined) {
      throw new Error("持仓公开投影与账户币种范围不一致");
    }
    // 单行数量、历史成本或方向异常只降级该行：本层对缺价已经宽容，不能因为一行坏数据
    // 让整份组合估值 500。降级行不发请求，并按 unsupported_instrument 标的不可估值。
    const valid = source.quantity.isFinite() && source.quantity.gt(0)
      && source.cost.isFinite() && !source.cost.lt(0)
      && (source.direction === "long" || source.direction === "short");
    const instrument = valid && currency === "USD" ? positionInstrument(source) : null;
    return { position, source, currency, instrument, valid };
  });
  const instruments = new Map<string, MarketInstrument>();
  for (const { instrument } of prepared) {
    if (instrument !== null) instruments.set(marketInstrumentKey(instrument), instrument);
  }
  // A source switch during a queued portfolio read must not mix routing revisions.
  const snapshot = reader.snapshot?.() ?? reader;
  const lookups = await readQuotes(instruments, snapshot);
  const rows: MarketPosition[] = prepared.map(({ position, source, currency, instrument, valid }) => {
    const instrumentKey = instrument === null ? null : marketInstrumentKey(instrument);
    const quote = instrumentKey === null
      ? unavailable(valid && currency !== "USD" ? "currency_unsupported" : "unsupported_instrument")
      : lookups.get(instrumentKey) ?? unavailable("provider_unavailable");
    const multiplier = instrument === null ? null : instrument.assetType === "option" ? "100" : "1";
    let marketValue: string | null = null;
    let unrealizedPnl: string | null = null;
    if (quote.status !== "unavailable" && quote.referencePrice !== null && multiplier !== null) {
      const sign = source.direction === "long" ? 1 : -1;
      const quotedUnits = new Money(source.quantity).mul(quote.referencePrice.value);
      marketValue = quotedUnits.mul(multiplier).mul(sign).toFixed();
      unrealizedPnl = quotedUnits.minus(source.cost).mul(multiplier).mul(sign).toFixed();
    }
    return {
      ...position,
      currency,
      instrumentKey,
      quote,
      valuation: {
        marketValue,
        unrealizedPnl,
        multiplier,
        basis: "reference_price_excluding_fees",
      },
    };
  });
  return {
    rows,
    count: rows.length,
    pricedCount: rows.filter(hasValuation).length,
    complete: rows.every(isComplete),
    basis: "current_positions_at_quote_times_excluding_fees",
    marketData: snapshot.status(),
    currencyGroups: scope.currencyGroups.map((group) => {
      const accounts = new Set(group.accountIds);
      const selected = rows.filter((row) => accounts.has(row.accountId));
      const priced = selected.filter(hasValuation);
      const complete = selected.every(isComplete);
      const dates = priced.map((row) => new Date(row.quote.referencePrice!.asOf).toISOString()).sort();
      return {
        currency: group.currency,
        accountIds: [...group.accountIds],
        positions: selected.length,
        pricedPositions: priced.length,
        complete,
        marketValue: complete ? sum(priced, "marketValue") : null,
        unrealizedPnl: complete ? sum(priced, "unrealizedPnl") : null,
        asOfFrom: dates[0] ?? null,
        asOfTo: dates.at(-1) ?? null,
      };
    }),
  };
}

/** 账户币种只能来自已解析的服务端范围，不能根据报价或 ticker 猜测。 */
function accountCurrencies(scope: ResolvedAccountScope): ReadonlyMap<string, string> {
  const authorized = new Set(scope.accountIds);
  const currencies = new Map<string, string>();
  for (const group of scope.currencyGroups) {
    for (const accountId of group.accountIds) {
      if (!authorized.has(accountId) || currencies.has(accountId)) {
        throw new Error("账户币种分组包含越界或重复账户");
      }
      currencies.set(accountId, group.currency);
    }
  }
  if (currencies.size !== authorized.size) throw new Error("账户币种分组不完整");
  return currencies;
}

/** 当前账本没有调整合约交割信息，因此只接受可精确对应的标准 US 证券。 */
function positionInstrument(position: PositionRow): MarketInstrument | null {
  const symbol = position.symbolKey?.trim().toUpperCase();
  if (!symbol) return null;
  const underlying = position.underlying?.trim().toUpperCase();
  if (position.assetType === "stock") {
    if (!STOCK_SYMBOL.test(symbol) || (underlying !== undefined && underlying !== symbol)
      || position.expiry !== null || position.strike !== null || position.optionType !== null) return null;
    return { assetType: "stock", market: "US", symbol, currency: "USD" };
  }
  if (!underlying || !STANDARD_OPTION_ROOT.test(underlying) || position.expiry === null
    || position.strike === null || !position.strike.isFinite() || !position.strike.gt(0)
    || position.strike.gt("99999.999") || position.strike.decimalPlaces() > 3
    || !position.quantity.isInteger()
    || (position.optionType !== "C" && position.optionType !== "P")) return null;
  try {
    const date = parseIsoDate(position.expiry);
    if (date.getUTCFullYear() < 2000 || date.getUTCFullYear() > 2099) return null;
    const parsed = parseOptionSymbol(symbol);
    if (parsed === null || parsed.underlying !== underlying || parsed.expiry !== position.expiry
      || !parsed.strike.eq(position.strike) || parsed.optionType !== position.optionType) return null;
  } catch {
    return null;
  }
  return {
    assetType: "option",
    market: "US",
    underlying,
    expiry: position.expiry,
    strike: position.strike.toFixed(),
    optionType: position.optionType,
    multiplier: "100",
    currency: "USD",
  };
}

/** 同一证券在多个账户只取一个快照；对唯一证券的并发读取最多四个。 */
async function readQuotes(
  instruments: ReadonlyMap<string, MarketInstrument>,
  reader: MarketDataReader,
): Promise<ReadonlyMap<string, MarketQuoteLookup>> {
  const entries = [...instruments.entries()];
  const results = new Map<string, MarketQuoteLookup>();
  let cursor = 0;
  const worker = async (): Promise<void> => {
    while (cursor < entries.length) {
      const entry = entries[cursor++];
      if (entry === undefined) return;
      const [key, instrument] = entry;
      try {
        results.set(key, checkedLookup(key, await reader.getQuote(instrument)));
      } catch {
        // Reader 的意外失败也不能把供应商消息、URL 或密钥带入公开结果。
        results.set(key, unavailable("provider_unavailable"));
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(MAX_CONCURRENT_QUOTES, entries.length) }, worker));
  return results;
}

/** 不重新选价，仅守住关联身份、计价币种及可计算参考价的边界。 */
function checkedLookup(key: string, lookup: MarketQuoteLookup): MarketQuoteLookup {
  if (lookup.status === "unavailable") return { ...lookup, quote: null, referencePrice: null };
  if (lookup.quote === null || lookup.referencePrice === null
    || lookup.quote.instrumentKey !== key || lookup.quote.currency !== "USD") {
    return unavailable("invalid_response");
  }
  const price = new Money(lookup.referencePrice.value);
  const timestamp = lookup.referencePrice.asOf;
  if (!price.isFinite() || price.lt(0) || !/(?:Z|[+-]\d{2}:\d{2})$/u.test(timestamp)
    || !Number.isFinite(Date.parse(timestamp))) return unavailable("invalid_response");
  return lookup;
}

function unavailable(reason: MarketQuoteIssue): MarketQuoteLookup {
  return { status: "unavailable", quote: null, referencePrice: null, issue: reason, cached: false };
}

function hasValuation(row: MarketPosition): boolean {
  return row.valuation.marketValue !== null && row.valuation.unrealizedPnl !== null;
}

function isComplete(row: MarketPosition): boolean {
  return row.quote.status === "available" && hasValuation(row);
}

function sum(rows: readonly MarketPosition[], field: "marketValue" | "unrealizedPnl"): string {
  return rows.reduce((total, row) => total.plus(row.valuation[field]!), new Money(0)).toFixed();
}
