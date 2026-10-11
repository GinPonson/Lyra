import { z } from "zod";

import { MarketDataService, type MarketProviderPolicy } from "../../application/market-data-service.js";
import type { MarketDataProvider } from "../../domain/market-data/types.js";
import { AlpacaMarketDataProvider } from "./alpaca-provider.js";
import { MarketDataAppProvider } from "./marketdata-provider.js";

/** This composition root is the only place where application routing names concrete vendors. */
const PROVIDER_IDS = ["alpaca", "marketdata"] as const;
type ProviderId = typeof PROVIDER_IDS[number];

const routeSchema = z.string().trim().default("none").transform((input, context): ProviderId[] => {
  if (input === "none" || input === "") return [];
  const ids = input.split(",").map((id) => id.trim());
  if (new Set(ids).size !== ids.length || ids.some((id) => !(PROVIDER_IDS as readonly string[]).includes(id))) {
    context.addIssue({ code: "custom", message: "Invalid provider route" });
    return z.NEVER;
  }
  return ids as ProviderId[];
});
const secretSchema = z.string().trim().max(8_192).regex(/^[^\r\n]*$/).optional();

/** Shared by startup and the offline configuration checker; never performs network requests. */
export const marketDataEnvironmentSchema = z.object({
  LYRA_MARKET_STOCK_PROVIDERS: routeSchema,
  LYRA_MARKET_OPTION_PROVIDERS: routeSchema,
  LYRA_ALPACA_API_KEY: secretSchema,
  LYRA_ALPACA_SECRET_KEY: secretSchema,
  LYRA_MARKETDATA_API_TOKEN: secretSchema,
  LYRA_MARKETDATA_MODE: z.enum(["historical", "delayed"]).default("historical"),
  LYRA_MARKETDATA_DAILY_REQUEST_LIMIT: z.coerce.number().int().min(1).max(100_000).default(80),
  LYRA_MARKET_TIMEOUT_MS: z.coerce.number().int().min(1_000).max(15_000).default(8_000),
}).superRefine((environment, context) => {
  const routes = [...environment.LYRA_MARKET_STOCK_PROVIDERS, ...environment.LYRA_MARKET_OPTION_PROVIDERS];
  const requireValue = (name: "LYRA_ALPACA_API_KEY" | "LYRA_ALPACA_SECRET_KEY" | "LYRA_MARKETDATA_API_TOKEN"): void => {
    if (!environment[name]) context.addIssue({ code: "custom", path: [name], message: "Selected provider requires credentials" });
  };
  if (routes.includes("alpaca") || Boolean(environment.LYRA_ALPACA_API_KEY) !== Boolean(environment.LYRA_ALPACA_SECRET_KEY)) {
    requireValue("LYRA_ALPACA_API_KEY");
    requireValue("LYRA_ALPACA_SECRET_KEY");
  }
  if (routes.includes("marketdata")) requireValue("LYRA_MARKETDATA_API_TOKEN");
});

export const MARKET_ENVIRONMENT_VARIABLES = [
  "LYRA_MARKET_STOCK_PROVIDERS", "LYRA_MARKET_OPTION_PROVIDERS",
  "LYRA_ALPACA_API_KEY", "LYRA_ALPACA_SECRET_KEY", "LYRA_MARKETDATA_API_TOKEN",
  "LYRA_MARKETDATA_MODE", "LYRA_MARKETDATA_DAILY_REQUEST_LIMIT", "LYRA_MARKET_TIMEOUT_MS",
] as const;

/** No credentials means disabled, and unused credentials do not silently turn on remote access. */
export function createMarketDataServiceFromEnvironment(
  environment: NodeJS.ProcessEnv,
  dependencies: { fetchFn?: typeof globalThis.fetch; now?: () => Date } = {},
): MarketDataService {
  const config = marketDataEnvironmentSchema.parse(environment);
  const selected = new Set([...config.LYRA_MARKET_STOCK_PROVIDERS, ...config.LYRA_MARKET_OPTION_PROVIDERS]);
  const providers: MarketDataProvider[] = [];
  const policies: Record<string, MarketProviderPolicy> = {};
  const fetchDependency = dependencies.fetchFn === undefined ? {} : { fetchFn: dependencies.fetchFn };
  if (selected.has("alpaca")) {
    providers.push(new AlpacaMarketDataProvider({
      apiKey: config.LYRA_ALPACA_API_KEY!, secretKey: config.LYRA_ALPACA_SECRET_KEY!, ...fetchDependency,
    }));
    policies.alpaca = { cacheTtlMs: 15 * 60_000, maxRequestsPerMinute: 120, maxRequestsPerDay: null };
  }
  if (selected.has("marketdata")) {
    const historical = config.LYRA_MARKETDATA_MODE === "historical";
    providers.push(new MarketDataAppProvider({
      apiToken: config.LYRA_MARKETDATA_API_TOKEN!,
      timing: { kind: historical ? "end_of_day" : "delayed", delayMinutes: historical ? null : 15 },
      ...fetchDependency,
    }));
    policies.marketdata = {
      cacheTtlMs: historical ? 86_400_000 : 15 * 60_000,
      maxRequestsPerMinute: 30,
      maxRequestsPerDay: config.LYRA_MARKETDATA_DAILY_REQUEST_LIMIT,
    };
  }
  return new MarketDataService({
    providers,
    policies,
    routes: { stock: config.LYRA_MARKET_STOCK_PROVIDERS, option: config.LYRA_MARKET_OPTION_PROVIDERS },
    timeoutMs: config.LYRA_MARKET_TIMEOUT_MS,
    ...(dependencies.now === undefined ? {} : { now: dependencies.now }),
  });
}
