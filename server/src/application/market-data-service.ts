import { isCrossedMarket, selectMarketReferencePrice } from "../domain/market-data/reference-price.js";
import type { MarketRouteController, MarketRoutes } from "../domain/market-data/settings.js";
import {
  marketInstrumentKey,
  MARKET_QUOTE_ISSUES,
  type MarketAssetType,
  type MarketDataProvider,
  type MarketDataReader,
  type MarketDataStatus,
  type MarketInstrument,
  type MarketQuote,
  type MarketQuoteIssue,
  type MarketQuoteLookup,
  type ProviderQuoteResult,
} from "../domain/market-data/types.js";

/** Operational limits belong to the application, independently of vendor payload parsing. */
export interface MarketProviderPolicy {
  cacheTtlMs: number;
  maxRequestsPerMinute: number;
  /** Conservative rolling 24-hour request budget, not a promise about vendor credits. */
  maxRequestsPerDay: number | null;
}

export interface MarketDataServiceOptions {
  providers: readonly MarketDataProvider[];
  routes: Readonly<Record<MarketAssetType, readonly string[]>>;
  policies: Readonly<Record<string, MarketProviderPolicy>>;
  now?: () => Date;
  timeoutMs?: number;
  maxCacheEntries?: number;
  maxQuoteAgeMs?: number;
  concurrency?: number;
}

interface CacheEntry {
  quote: MarketQuote | null;
  issue: MarketQuoteIssue | null;
  expiresAt: number;
}

interface ProviderBudget {
  minuteCalls: number[];
  dailyCalls: number[];
  blockedUntil: number;
  blockedReason: MarketQuoteIssue;
}

const DAY_MS = 86_400_000;

/**
 * 只影响单个证券的失败状态。
 *
 * 这些状态来自单证券的数据缺失、标的不支持或报价形状校验，必须只短期缓存该证券；
 * 若拿去冷却整个来源，一个坏符号会连带让同来源的其他符号在冷却期内全部失败。
 * 供应商级冷却只保留传输、鉴权和限流状态。
 */
const PER_INSTRUMENT_ISSUES: readonly MarketQuoteIssue[] = [
  "no_data", "unsupported_instrument", "currency_unsupported", "invalid_response",
];

/**
 * A replaceable provider registry with explicit routing, request coalescing and bounded cache.
 * Only public instrument quotes are cached; account holdings and valuation results are not.
 * All network failures remain data states, so a missing feed never breaks the ledger.
 */
export class MarketDataService implements MarketDataReader, MarketRouteController {
  private readonly providers = new Map<string, MarketDataProvider>();
  private readonly budgets = new Map<string, ProviderBudget>();
  private readonly cache = new Map<string, CacheEntry>();
  private readonly inFlight = new Map<string, Promise<CacheEntry>>();
  private routes: MarketRoutes;
  private settingsRevision = 0;
  private readonly policies: Readonly<Record<string, MarketProviderPolicy>>;
  private readonly now: () => Date;
  private readonly timeoutMs: number;
  private readonly maxCacheEntries: number;
  private readonly maxQuoteAgeMs: number;
  private readonly concurrency: number;
  private activeRequests = 0;
  private readonly waiters: (() => void)[] = [];

  constructor(options: MarketDataServiceOptions) {
    this.routes = { stock: [...options.routes.stock], option: [...options.routes.option] };
    this.policies = structuredClone(options.policies);
    this.now = options.now ?? (() => new Date());
    this.timeoutMs = options.timeoutMs ?? 8_000;
    this.maxCacheEntries = options.maxCacheEntries ?? 1_000;
    this.maxQuoteAgeMs = options.maxQuoteAgeMs ?? 7 * DAY_MS;
    this.concurrency = options.concurrency ?? 4;
    if (![this.timeoutMs, this.maxCacheEntries, this.maxQuoteAgeMs, this.concurrency]
      .every((value) => Number.isInteger(value) && value > 0)) {
      throw new RangeError("Invalid market data service limits");
    }
    for (const provider of options.providers) {
      const policy = this.policies[provider.id];
      if (this.providers.has(provider.id) || policy === undefined
        || !Number.isInteger(policy.cacheTtlMs) || policy.cacheTtlMs <= 0
        || !Number.isInteger(policy.maxRequestsPerMinute) || policy.maxRequestsPerMinute <= 0
        || (policy.maxRequestsPerDay !== null
          && (!Number.isInteger(policy.maxRequestsPerDay) || policy.maxRequestsPerDay <= 0))) {
        throw new RangeError("Invalid market data provider policy");
      }
      this.providers.set(provider.id, provider);
      this.budgets.set(provider.id, {
        minuteCalls: [], dailyCalls: [], blockedUntil: 0, blockedReason: "provider_unavailable",
      });
    }
    this.validateRoutes(this.routes);
  }

  status(): MarketDataStatus {
    return this.describeRoutes(this.routes, this.settingsRevision);
  }

  /** Keep per-provider quotas and caches; a UI switch must not reset consumption limits. */
  setRoutes(routes: MarketRoutes, revision: number): void {
    if (!Number.isSafeInteger(revision) || revision < this.settingsRevision) {
      throw new RangeError("Invalid market settings revision");
    }
    this.validateRoutes(routes);
    this.routes = { stock: [...routes.stock], option: [...routes.option] };
    this.settingsRevision = revision;
  }

  snapshot(): MarketDataReader {
    const routes: MarketRoutes = { stock: [...this.routes.stock], option: [...this.routes.option] };
    const status = this.describeRoutes(routes, this.settingsRevision);
    return {
      status: () => structuredClone(status),
      getQuote: (instrument) => this.readQuote(instrument, routes[instrument.assetType]),
    };
  }

  private validateRoutes(routes: MarketRoutes): void {
    for (const assetType of ["stock", "option"] as const) {
      if (!Array.isArray(routes[assetType]) || new Set(routes[assetType]).size !== routes[assetType].length
        || routes[assetType].some((id) => !this.providers.get(id)?.assetTypes.includes(assetType))) {
        throw new RangeError("Invalid market data provider route");
      }
    }
  }

  private describeRoutes(routes: MarketRoutes, revision: number): MarketDataStatus {
    return {
      enabled: routes.stock.length + routes.option.length > 0,
      settingsRevision: revision,
      routes: { stock: [...routes.stock], option: [...routes.option] },
      providers: [...this.providers.values()].map((provider) => ({
        id: provider.id,
        assetTypes: [...provider.assetTypes],
        cacheTtlSeconds: this.policies[provider.id]!.cacheTtlMs / 1_000,
      })),
    };
  }

  async getQuote(instrument: MarketInstrument): Promise<MarketQuoteLookup> {
    return this.readQuote(instrument, this.routes[instrument.assetType]);
  }

  private async readQuote(instrument: MarketInstrument, route: readonly string[]): Promise<MarketQuoteLookup> {
    let issue: MarketQuoteIssue = "not_configured";
    let stale: MarketQuoteLookup | null = null;
    for (const providerId of route) {
      const key = JSON.stringify([providerId, marketInstrumentKey(instrument)]);
      const existing = this.cache.get(key);
      const cached = existing !== undefined && existing.expiresAt > this.now().getTime();
      let entry: CacheEntry;
      if (cached) {
        entry = existing;
      } else {
        let pending = this.inFlight.get(key);
        if (pending === undefined) {
          pending = this.refresh(providerId, instrument, key, existing);
          this.inFlight.set(key, pending);
        }
        try {
          entry = await pending;
        } finally {
          if (this.inFlight.get(key) === pending) this.inFlight.delete(key);
        }
      }
      issue = entry.issue ?? "no_data";
      if (entry.quote === null) continue;
      const referencePrice = selectMarketReferencePrice(entry.quote);
      if (referencePrice === null) continue;
      const tooOld = this.now().getTime() - Date.parse(referencePrice.asOf) > this.maxQuoteAgeMs;
      const result: MarketQuoteLookup = {
        status: entry.issue === null && !tooOld ? "available" : "stale",
        quote: structuredClone(entry.quote),
        referencePrice,
        issue: entry.issue,
        cached,
      };
      if (result.status === "available") return result;
      // A healthy explicitly configured fallback wins over a failed primary's stale cache.
      stale ??= result;
    }
    return stale ?? { status: "unavailable", quote: null, referencePrice: null, issue, cached: false };
  }

  private async refresh(
    providerId: string,
    instrument: MarketInstrument,
    key: string,
    previous: CacheEntry | undefined,
  ): Promise<CacheEntry> {
    await this.acquire();
    try {
      const now = this.now();
      const denied = this.consumeBudget(providerId, now.getTime());
      const result = denied === null
        ? await this.request(this.providers.get(providerId)!, instrument, now)
        : { status: "unavailable" as const, reason: denied };
      const completedAt = this.now().getTime();
      let entry: CacheEntry;
      if (result.status === "available") {
        entry = {
          quote: structuredClone(result.quote),
          issue: null,
          expiresAt: completedAt + this.policies[providerId]!.cacheTtlMs,
        };
      } else {
        const cooldown = result.reason === "rate_limited" ? 15 * 60_000
          : result.reason === "credentials_rejected" ? 5 * 60_000
            : result.reason === "no_data" || result.reason === "unsupported_instrument" ? 15 * 60_000
              : 60_000;
        // Per-symbol absence, unsupported identity or malformed quote must not disable a whole provider.
        if (denied === null && !PER_INSTRUMENT_ISSUES.includes(result.reason)) {
          const budget = this.budgets.get(providerId)!;
          budget.blockedUntil = Math.max(budget.blockedUntil, completedAt + cooldown);
          budget.blockedReason = result.reason;
        }
        entry = { quote: previous?.quote ?? null, issue: result.reason, expiresAt: completedAt + cooldown };
      }
      this.cache.delete(key);
      this.cache.set(key, entry);
      while (this.cache.size > this.maxCacheEntries) {
        const oldest = this.cache.keys().next().value;
        if (oldest === undefined) break;
        this.cache.delete(oldest);
      }
      return entry;
    } finally {
      this.release();
    }
  }

  private async request(provider: MarketDataProvider, instrument: MarketInstrument, now: Date): Promise<ProviderQuoteResult> {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const timeout = new Promise<ProviderQuoteResult>((resolve) => {
        timer = setTimeout(() => {
          controller.abort();
          resolve({ status: "unavailable", reason: "timeout" });
        }, this.timeoutMs);
      });
      const pending = Promise.resolve().then(() => provider.fetchQuote(instrument, { now, signal: controller.signal }));
      const result: unknown = await Promise.race([pending, timeout]);
      if (isRecord(result) && result.status === "unavailable"
        && typeof result.reason === "string" && (MARKET_QUOTE_ISSUES as readonly string[]).includes(result.reason)) {
        return { status: "unavailable", reason: result.reason as MarketQuoteIssue };
      }
      if (!isRecord(result) || result.status !== "available") {
        return { status: "unavailable", reason: "invalid_response" };
      }
      const normalized = normalizeQuote(result.quote, instrument, provider.id, this.now());
      return normalized.ok
        ? { status: "available", quote: normalized.quote }
        : { status: "unavailable", reason: normalized.reason };
    } catch {
      return { status: "unavailable", reason: controller.signal.aborted ? "timeout" : "provider_unavailable" };
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
  }

  private consumeBudget(providerId: string, timestamp: number): MarketQuoteIssue | null {
    const policy = this.policies[providerId]!;
    const budget = this.budgets.get(providerId)!;
    if (budget.blockedUntil > timestamp) return budget.blockedReason;
    budget.minuteCalls = budget.minuteCalls.filter((calledAt) => timestamp - calledAt < 60_000);
    budget.dailyCalls = budget.dailyCalls.filter((calledAt) => timestamp - calledAt < DAY_MS);
    if (budget.minuteCalls.length >= policy.maxRequestsPerMinute
      || (policy.maxRequestsPerDay !== null && budget.dailyCalls.length >= policy.maxRequestsPerDay)) {
      return "rate_limited";
    }
    budget.minuteCalls.push(timestamp);
    if (policy.maxRequestsPerDay !== null) budget.dailyCalls.push(timestamp);
    return null;
  }

  private async acquire(): Promise<void> {
    if (this.activeRequests >= this.concurrency) {
      await new Promise<void>((resolve) => this.waiters.push(resolve));
    } else {
      this.activeRequests += 1;
    }
  }

  private release(): void {
    const next = this.waiters.shift();
    if (next === undefined) this.activeRequests -= 1;
    else next();
  }
}

/** 归一化结果：`invalid_response` 表示报价形状/身份不合法，`no_data` 表示形状合法但无可用参考价。 */
type NormalizedQuote =
  | { ok: true; quote: MarketQuote }
  | { ok: false; reason: "invalid_response" | "no_data" };

function normalizeQuote(
  raw: unknown,
  instrument: MarketInstrument,
  providerId: string,
  now: Date,
): NormalizedQuote {
  if (!isRecord(raw) || raw.instrumentKey !== marketInstrumentKey(instrument) || raw.providerId !== providerId
    || raw.currency !== instrument.currency || typeof raw.feed !== "string" || raw.feed.length < 1 || raw.feed.length > 100
    || !validTimestamp(raw.fetchedAt, now) || !isRecord(raw.timing)
    || (raw.timing.kind !== "delayed" && raw.timing.kind !== "end_of_day")
    || (raw.timing.delayMinutes !== null
      && (typeof raw.timing.delayMinutes !== "number" || !Number.isFinite(raw.timing.delayMinutes) || raw.timing.delayMinutes < 0))) {
    return { ok: false, reason: "invalid_response" };
  }
  const points: Pick<MarketQuote, "bid" | "ask" | "last" | "close"> = { bid: null, ask: null, last: null, close: null };
  for (const field of ["bid", "ask", "last", "close"] as const) {
    const point = raw[field];
    if (point === null) continue;
    if (!isRecord(point) || typeof point.value !== "string"
      || !/^(?:0|[1-9]\d{0,15})(?:\.\d{1,12})?$/.test(point.value)
      || !validTimestamp(point.asOf, now)) return { ok: false, reason: "invalid_response" };
    points[field] = { value: point.value, asOf: new Date(point.asOf).toISOString() };
  }
  // Explicit projection strips unknown vendor fields (including accidental credential fields).
  const quote: MarketQuote = {
    instrumentKey: raw.instrumentKey, providerId: raw.providerId, currency: "USD", feed: raw.feed,
    ...points,
    timing: { kind: raw.timing.kind, delayMinutes: raw.timing.delayMinutes },
    fetchedAt: new Date(raw.fetchedAt).toISOString(),
  };
  // 交叉盘口是供应商数据形状错误，必须保留 invalid_response，不能混同为“没有数据”。
  if (isCrossedMarket(quote)) return { ok: false, reason: "invalid_response" };
  // 形状完全合法却挑不出参考价（例如只有买价的单边盘口）属于缺数据，报 no_data 而非 invalid_response。
  return selectMarketReferencePrice(quote) === null
    ? { ok: false, reason: "no_data" }
    : { ok: true, quote };
}

function validTimestamp(timestamp: unknown, now: Date): timestamp is string {
  if (typeof timestamp !== "string"
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/.test(timestamp)) return false;
  const year = Number(timestamp.slice(0, 4));
  const month = Number(timestamp.slice(5, 7));
  const day = Number(timestamp.slice(8, 10));
  return month >= 1 && month <= 12 && day >= 1 && day <= new Date(Date.UTC(year, month, 0)).getUTCDate()
    && Number.isFinite(Date.parse(timestamp)) && Date.parse(timestamp) >= 0
    && Date.parse(timestamp) <= now.getTime() + 60_000;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
