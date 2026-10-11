import type { MarketAssetType, MarketDataStatus, MarketQuote } from "./types.js";

export type MarketRoutes = Readonly<Record<MarketAssetType, readonly string[]>>;

/** Public configuration availability; never a remote health or entitlement guarantee. */
export interface MarketProviderOption {
  id: string;
  displayName: string;
  assetTypes: readonly MarketAssetType[];
  configured: boolean;
  timing: MarketQuote["timing"];
  cacheTtlSeconds: number;
}

/** Owner-wide settings shared by the App and the read-only Genkit market tool. */
export interface MarketSettingsView {
  revision: number;
  routes: MarketRoutes;
  effectiveRoutes: MarketRoutes;
  providers: readonly MarketProviderOption[];
  updatedAt: string | null;
}

/** The application may replace routes without recreating providers, caches or budgets. */
export interface MarketRouteController {
  status(): MarketDataStatus;
  setRoutes(routes: MarketRoutes, revision: number): void;
}
