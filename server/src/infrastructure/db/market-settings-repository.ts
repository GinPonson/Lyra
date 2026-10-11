import { z } from "zod";

import { ApplicationError } from "../../application/app-error.js";
import type { MarketRoutes } from "../../domain/market-data/settings.js";
import type { LyraDatabase } from "./database.js";

const providerId = z.string().regex(/^[a-z][a-z0-9_-]{0,63}$/u);
const route = z.array(providerId).max(2).refine((ids) => new Set(ids).size === ids.length);

/** The same closed shape is accepted from the App and from persisted records. */
export const marketRoutesSchema = z.object({ stock: route, option: route }).strict();

export interface StoredMarketSettings {
  revision: number;
  routes: MarketRoutes;
  updatedAt: string;
}

/** Missing means deployment defaults; malformed records must never become defaults. */
export async function loadMarketSettings(database: LyraDatabase): Promise<StoredMarketSettings | null> {
  const reader = await database.connection.runAndReadAll(`
    SELECT singleton_key, revision, CAST(routes_json AS VARCHAR) AS routes_json,
      CAST(updated_at AS VARCHAR) AS updated_at
    FROM market_source_settings
  `);
  const rows = reader.getRowObjectsJson();
  if (rows.length === 0) return null;
  if (rows.length !== 1 || rows[0]!.singleton_key !== 1) {
    throw new Error("行情设置持久记录无效，未使用默认值覆盖");
  }
  const row = rows[0]!;
  const revision = parseStoredRevision(row.revision);
  if (typeof row.routes_json !== "string" || typeof row.updated_at !== "string") {
    throw new Error("行情设置持久记录字段无效，未使用默认值覆盖");
  }
  let decoded: unknown;
  try {
    decoded = JSON.parse(row.routes_json);
  } catch {
    throw new Error("行情设置持久记录 JSON 无效，未使用默认值覆盖");
  }
  const routes = marketRoutesSchema.safeParse(decoded);
  const timestamp = Date.parse(row.updated_at);
  if (!routes.success || !Number.isFinite(timestamp)) {
    throw new Error("行情设置持久记录内容无效，未使用默认值覆盖");
  }
  return { revision, routes: routes.data, updatedAt: new Date(timestamp).toISOString() };
}

/** Caller owns the shared write queue. The runtime changes only after this commits. */
export async function persistMarketSettings(
  database: LyraDatabase,
  input: { expectedRevision: number; settings: StoredMarketSettings },
): Promise<void> {
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    const stored = await loadMarketSettings(database);
    if ((stored?.revision ?? 0) !== input.expectedRevision) {
      throw new ApplicationError({
        code: "MARKET_SETTINGS_CHANGED",
        message: "行情设置已在其他设备更新，请重新读取后再保存",
        statusCode: 409,
        retryable: false,
      });
    }
    const settings = input.settings;
    await connection.run(`
      INSERT INTO market_source_settings (singleton_key, revision, routes_json, updated_at)
      VALUES (1, $revision, CAST($routes_json AS JSON), CAST($updated_at AS TIMESTAMPTZ))
      ON CONFLICT (singleton_key) DO UPDATE SET
        revision = excluded.revision,
        routes_json = excluded.routes_json,
        updated_at = excluded.updated_at
    `, {
      revision: settings.revision,
      routes_json: JSON.stringify(settings.routes),
      updated_at: settings.updatedAt,
    });
    await connection.run("COMMIT");
  } catch (error: unknown) {
    try {
      await connection.run("ROLLBACK");
    } catch (rollbackError: unknown) {
      throw new AggregateError([error, rollbackError], "行情设置保存失败且回滚失败");
    }
    throw error;
  }
}

function parseStoredRevision(value: unknown): number {
  const revision = typeof value === "number"
    ? value
    : typeof value === "string" && /^\d+$/u.test(value) ? Number(value) : Number.NaN;
  if (!Number.isSafeInteger(revision) || revision < 1) {
    throw new Error("行情设置持久版本无效，未使用默认值覆盖");
  }
  return revision;
}
