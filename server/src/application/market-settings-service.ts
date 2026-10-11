import { z } from "zod";

import type {
  MarketProviderOption,
  MarketRouteController,
  MarketRoutes,
  MarketSettingsView,
} from "../domain/market-data/settings.js";
import type { MarketAssetType } from "../domain/market-data/types.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";
import {
  loadMarketSettings,
  marketRoutesSchema,
  persistMarketSettings,
} from "../infrastructure/db/market-settings-repository.js";
import { ApplicationError } from "./app-error.js";
import type { WriteCoordinator } from "./write-coordinator.js";

const saveSchema = z.object({
  expectedRevision: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
  routes: marketRoutesSchema,
}).strict();

const ASSET_TYPES: readonly MarketAssetType[] = ["stock", "option"];

/** Owner settings are an authenticated App mutation, never a writable Genkit tool. */
export class MarketSettingsService {
  readonly #database: LyraDatabase;
  readonly #writeCoordinator: WriteCoordinator;
  readonly #marketData: MarketRouteController;
  readonly #providers: readonly MarketProviderOption[];
  readonly #providerById: ReadonlyMap<string, MarketProviderOption>;
  readonly #defaultRoutes: MarketRoutes;
  readonly #now: () => Date;
  #state: MarketSettingsView | null = null;
  #initialization: Promise<void> | null = null;

  constructor(input: {
    database: LyraDatabase;
    writeCoordinator: WriteCoordinator;
    marketData: MarketRouteController;
    providers: readonly MarketProviderOption[];
    defaultRoutes: MarketRoutes;
    now?: () => Date;
  }) {
    this.#database = input.database;
    this.#writeCoordinator = input.writeCoordinator;
    this.#marketData = input.marketData;
    this.#providers = input.providers.map((provider) => ({
      id: provider.id,
      displayName: provider.displayName,
      assetTypes: [...provider.assetTypes],
      configured: provider.configured,
      timing: { kind: provider.timing.kind, delayMinutes: provider.timing.delayMinutes },
      cacheTtlSeconds: provider.cacheTtlSeconds,
    }));
    this.#providerById = new Map(this.#providers.map((provider) => [provider.id, provider]));
    if (this.#providerById.size !== this.#providers.length) {
      throw new Error("行情来源目录存在重复标识");
    }
    const defaults = marketRoutesSchema.safeParse(input.defaultRoutes);
    if (!defaults.success) throw new Error("行情默认来源配置格式无效");
    this.#defaultRoutes = defaults.data;
    this.#assertKnownRoutes(this.#defaultRoutes);
    this.#now = input.now ?? (() => new Date());
  }

  /** Restore before the HTTP listener or the read-only tools accept requests. */
  async initialize(): Promise<void> {
    this.#initialization ??= this.#writeCoordinator.enqueue(async () => {
      const state = await this.#readStoredView();
      this.#apply(state);
    });
    await this.#initialization;
  }

  /** A committed memory snapshot avoids reading another write's open transaction. */
  getSettings(): MarketSettingsView {
    if (this.#state === null) throw new Error("行情设置尚未初始化");
    return structuredClone(this.#state);
  }

  async save(input: {
    request: unknown;
    requestId: string;
    authorize: () => Promise<{ id: string }>;
  }): Promise<MarketSettingsView> {
    const parsed = saveSchema.safeParse(input.request);
    if (!parsed.success) {
      throw new ApplicationError({
        code: "INVALID_ARGUMENT",
        message: "行情设置须包含有效版本；股票和期权各可选择最多两个不重复的来源",
        statusCode: 400,
        retryable: false,
      });
    }
    const request = parsed.data;
    return this.#writeCoordinator.enqueue(async () => {
      await input.authorize();
      if (this.#state === null) throw new Error("行情设置尚未初始化");
      this.#assertSelectableRoutes(request.routes);
      // Re-read inside the shared queue so an additional Service instance cannot
      // apply an old revision over a newer committed selection.
      const current = await this.#readStoredView();
      if (sameRoutes(request.routes, current.routes)) {
        if (this.#state.revision !== current.revision || !sameRoutes(this.#state.routes, current.routes)) {
          this.#apply(current);
        }
        return this.getSettings();
      }
      if (request.expectedRevision !== current.revision) {
        throw new ApplicationError({
          code: "MARKET_SETTINGS_CHANGED",
          message: "行情设置已在其他设备更新，请重新读取后再保存",
          statusCode: 409,
          retryable: false,
          details: { revision: current.revision },
        });
      }
      if (current.revision >= Number.MAX_SAFE_INTEGER) {
        throw new Error("行情设置版本已超出安全整数范围");
      }
      const settings = {
        revision: current.revision + 1,
        routes: request.routes,
        updatedAt: this.#now().toISOString(),
      };
      await persistMarketSettings(this.#database, { expectedRevision: current.revision, settings });
      this.#apply(this.#view(settings));
      return this.getSettings();
    });
  }

  async #readStoredView(): Promise<MarketSettingsView> {
    const stored = await loadMarketSettings(this.#database);
    return this.#view(stored ?? { revision: 0, routes: this.#defaultRoutes, updatedAt: null });
  }

  #view(input: { revision: number; routes: MarketRoutes; updatedAt: string | null }): MarketSettingsView {
    const effective = (assetType: MarketAssetType) => input.routes[assetType].filter((id) => {
      const provider = this.#providerById.get(id);
      return provider !== undefined && provider.configured && provider.assetTypes.includes(assetType);
    });
    return {
      revision: input.revision,
      routes: structuredClone(input.routes),
      effectiveRoutes: { stock: effective("stock"), option: effective("option") },
      providers: structuredClone(this.#providers),
      updatedAt: input.updatedAt,
    };
  }

  #apply(state: MarketSettingsView): void {
    this.#marketData.setRoutes(structuredClone(state.effectiveRoutes), state.revision);
    this.#state = state;
  }

  #assertKnownRoutes(routes: MarketRoutes): void {
    for (const assetType of ASSET_TYPES) {
      for (const id of routes[assetType]) {
        if (!this.#providerById.has(id)) {
          throw new Error("行情设置包含未知来源，未使用默认值覆盖");
        }
      }
    }
  }

  #assertSelectableRoutes(routes: MarketRoutes): void {
    for (const assetType of ASSET_TYPES) {
      for (const id of routes[assetType]) {
        const provider = this.#providerById.get(id);
        if (provider === undefined || !provider.assetTypes.includes(assetType)) {
          throw new ApplicationError({
            code: "INVALID_ARGUMENT",
            message: "所选行情来源不支持该资产类型",
            statusCode: 400,
            retryable: false,
          });
        }
        if (!provider.configured) {
          throw new ApplicationError({
            code: "MARKET_PROVIDER_NOT_CONFIGURED",
            message: "所选行情来源尚未在服务器配置凭据，请选择已配置的来源",
            statusCode: 400,
            retryable: false,
          });
        }
      }
    }
  }
}

function sameRoutes(left: MarketRoutes, right: MarketRoutes): boolean {
  return ASSET_TYPES.every((assetType) => left[assetType].length === right[assetType].length
    && left[assetType].every((id, index) => id === right[assetType][index]));
}
