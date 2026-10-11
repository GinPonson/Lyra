import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setImmediate } from "node:timers/promises";
import { describe, it, type TestContext } from "node:test";

import { ApplicationError } from "../src/application/app-error.js";
import { createDeviceAuthGate } from "../src/application/device-auth-gate.js";
import { DevicePairingService } from "../src/application/device-pairing-service.js";
import { MarketSettingsService } from "../src/application/market-settings-service.js";
import { WriteCoordinator } from "../src/application/write-coordinator.js";
import type {
  MarketProviderOption,
  MarketRouteController,
  MarketRoutes,
} from "../src/domain/market-data/settings.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";
import { loadMarketSettings } from "../src/infrastructure/db/market-settings-repository.js";
import { CURRENT_SCHEMA_VERSION } from "../src/infrastructure/db/schema-version.js";
import { buildHttpServer, createServerProtocolMetadata } from "../src/transport/index.js";
import { registerMarketSettingsRoutes } from "../src/transport/market-settings-routes.js";

const NOW = "2026-10-11T04:00:00.000Z";
const DEFAULT_ROUTES: MarketRoutes = { stock: ["alpaca"], option: ["marketdata"] };
const CHANGED_ROUTES: MarketRoutes = { stock: ["marketdata", "alpaca"], option: [] };
const PROVIDERS: readonly MarketProviderOption[] = [
  {
    id: "alpaca", displayName: "Alpaca", assetTypes: ["stock", "option"], configured: true,
    timing: { kind: "delayed", delayMinutes: 15 }, cacheTtlSeconds: 900,
  },
  {
    id: "marketdata", displayName: "MarketData", assetTypes: ["stock", "option"], configured: true,
    timing: { kind: "end_of_day", delayMinutes: null }, cacheTtlSeconds: 86_400,
  },
];
const PROTOCOL_HEADERS = { "x-lyra-protocol-version": "2" };

function createController() {
  const calls: { routes: MarketRoutes; revision: number }[] = [];
  let active: MarketRoutes = { stock: [], option: [] };
  let revision = 0;
  const controller: MarketRouteController = {
    status: () => ({
      enabled: active.stock.length > 0 || active.option.length > 0,
      routes: structuredClone(active), providers: [], revision,
    }),
    setRoutes: (routes, nextRevision) => {
      active = structuredClone(routes);
      revision = nextRevision;
      calls.push({ routes: structuredClone(routes), revision });
    },
  };
  return { controller, calls };
}

async function fixture(t: TestContext) {
  const directory = await mkdtemp(join(tmpdir(), "lyra-market-settings-"));
  const path = join(directory, "ledger.duckdb");
  const database = await openLyraDatabase(path);
  const writeCoordinator = new WriteCoordinator();
  const runtime = createController();
  const now = () => new Date(NOW);
  const service = new MarketSettingsService({
    database, writeCoordinator, marketData: runtime.controller,
    providers: PROVIDERS, defaultRoutes: DEFAULT_ROUTES, now,
  });
  await service.initialize();
  t.after(async () => {
    await writeCoordinator.close();
    database.close();
    await rm(directory, { recursive: true, force: true });
  });
  const authorize = async () => ({ id: "verified-owner-device" });
  const save = (routes: unknown, expectedRevision = 0) => service.save({
    request: { routes, expectedRevision }, requestId: "settings-test", authorize,
  });
  return { database, path, writeCoordinator, runtime, service, now, authorize, save };
}

function hasCode(code: string) {
  return (error: unknown) => error instanceof ApplicationError && error.code === code;
}

describe("persistent owner market settings", () => {
  it("uses deployment defaults only for an empty table and returns isolated public snapshots", async (t) => {
    const f = await fixture(t);
    assert.deepEqual(f.service.getSettings(), {
      revision: 0, routes: DEFAULT_ROUTES, effectiveRoutes: DEFAULT_ROUTES,
      providers: PROVIDERS, updatedAt: null,
    });
    assert.equal(await loadMarketSettings(f.database), null);
    await f.service.initialize();
    assert.equal(f.runtime.calls.length, 1);
    const exposed = f.service.getSettings();
    (exposed.routes.stock as string[]).push("injected");
    exposed.providers[0]!.configured = false;
    assert.deepEqual(f.service.getSettings().routes, DEFAULT_ROUTES);
    assert.equal(f.service.getSettings().providers[0]!.configured, true);
  });

  it("commits choices, restores through another service and a reopened DuckDB, and does not change the ledger", async (t) => {
    const f = await fixture(t);
    const saved = await f.save(CHANGED_ROUTES);
    assert.equal(saved.revision, 1);
    assert.equal(saved.updatedAt, NOW);
    assert.deepEqual(saved.routes, CHANGED_ROUTES);
    assert.deepEqual(f.runtime.calls.at(-1), { routes: CHANGED_ROUTES, revision: 1 });
    assert.deepEqual(await loadMarketSettings(f.database), { revision: 1, routes: CHANGED_ROUTES, updatedAt: NOW });
    assert.deepEqual(await f.database.readHealth(), { status: "available", schemaVersion: CURRENT_SCHEMA_VERSION, ledgerVersion: 0 });
    const another = new MarketSettingsService({
      database: f.database, writeCoordinator: f.writeCoordinator, marketData: createController().controller,
      providers: PROVIDERS, defaultRoutes: { stock: [], option: [] }, now: f.now,
    });
    await another.initialize();
    assert.deepEqual(another.getSettings(), saved);
    f.database.close();
    const reopened = await openLyraDatabase(f.path);
    try {
      const restored = new MarketSettingsService({
        database: reopened, writeCoordinator: f.writeCoordinator, marketData: createController().controller,
        providers: PROVIDERS, defaultRoutes: DEFAULT_ROUTES, now: f.now,
      });
      await restored.initialize();
      assert.deepEqual(restored.getSettings(), saved);
      assert.deepEqual(await reopened.readHealth(), { status: "available", schemaVersion: CURRENT_SCHEMA_VERSION, ledgerVersion: 0 });
    } finally {
      reopened.close();
    }
  });

  it("treats an unchanged choice and a lost-response retry as idempotent without advancing revision", async (t) => {
    const f = await fixture(t);
    assert.equal((await f.save(DEFAULT_ROUTES)).revision, 0);
    assert.equal(await loadMarketSettings(f.database), null);
    const changed = await f.save(CHANGED_ROUTES);
    assert.deepEqual(await f.save(CHANGED_ROUTES, 0), changed);
    assert.deepEqual(await f.save(CHANGED_ROUTES, 1), changed);
    assert.equal(f.runtime.calls.length, 2);
    const rows = await f.database.connection.runAndReadAll("SELECT count(*) AS count FROM market_source_settings");
    assert.deepEqual(rows.getRowObjectsJson(), [{ count: "1" }]);
  });

  it("serializes concurrent choices and rejects a stale different selection", async (t) => {
    const f = await fixture(t);
    const results = await Promise.allSettled([
      f.save(CHANGED_ROUTES),
      f.save({ stock: [], option: ["alpaca"] }),
    ]);
    assert.equal(results[0]!.status, "fulfilled");
    assert.equal(results[1]!.status, "rejected");
    const rejected = results[1] as PromiseRejectedResult;
    assert.ok(hasCode("MARKET_SETTINGS_CHANGED")(rejected.reason));
    assert.deepEqual(f.service.getSettings().routes, CHANGED_ROUTES);
    const updated = await f.save({ stock: [], option: ["alpaca"] }, 1);
    assert.equal(updated.revision, 2);
    assert.deepEqual(updated.routes, { stock: [], option: ["alpaca"] });
  });

  it("preserves selections when a key disappears and permits an authenticated repair", async (t) => {
    const f = await fixture(t);
    await f.save(CHANGED_ROUTES);
    const runtime = createController();
    const providers = PROVIDERS.map((provider) => ({ ...provider, configured: provider.id !== "marketdata" }));
    const restored = new MarketSettingsService({
      database: f.database, writeCoordinator: f.writeCoordinator, marketData: runtime.controller,
      providers, defaultRoutes: DEFAULT_ROUTES, now: f.now,
    });
    await restored.initialize();
    assert.deepEqual(restored.getSettings().routes, CHANGED_ROUTES);
    assert.deepEqual(restored.getSettings().effectiveRoutes, { stock: ["alpaca"], option: [] });
    assert.equal(restored.getSettings().revision, 1);
    await assert.rejects(restored.save({
      request: { expectedRevision: 1, routes: { stock: ["marketdata"], option: [] } },
      requestId: "unconfigured", authorize: f.authorize,
    }), hasCode("MARKET_PROVIDER_NOT_CONFIGURED"));
    const repaired = await restored.save({
      request: { expectedRevision: 1, routes: { stock: ["alpaca"], option: [] } },
      requestId: "repair", authorize: f.authorize,
    });
    assert.equal(repaired.revision, 2);
    assert.deepEqual(repaired.routes, repaired.effectiveRoutes);
  });

  it("preserves a removed adapter identifier for UI repair but never re-enables it", async (t) => {
    const f = await fixture(t);
    await f.save(CHANGED_ROUTES);
    await f.writeCoordinator.enqueue(() => f.database.connection.run(`
      UPDATE market_source_settings SET routes_json = '{"stock":["retired_vendor","alpaca"],"option":[]}'
    `));
    const runtime = createController();
    const restored = new MarketSettingsService({
      database: f.database, writeCoordinator: f.writeCoordinator, marketData: runtime.controller,
      providers: PROVIDERS, defaultRoutes: DEFAULT_ROUTES, now: f.now,
    });
    await restored.initialize();
    assert.deepEqual(restored.getSettings().routes.stock, ["retired_vendor", "alpaca"]);
    assert.deepEqual(restored.getSettings().effectiveRoutes.stock, ["alpaca"]);
    await assert.rejects(restored.save({
      request: { expectedRevision: 1, routes: { stock: ["retired_vendor"], option: [] } },
      requestId: "unknown", authorize: f.authorize,
    }), hasCode("INVALID_ARGUMENT"));
    assert.deepEqual((await loadMarketSettings(f.database))!.routes.stock, ["retired_vendor", "alpaca"]);
  });

  it("rejects malformed, duplicate, URL, key, unknown and unsupported source choices without writing", async (t) => {
    const f = await fixture(t);
    const invalid = [
      null, {}, [], { stock: "alpaca", option: [] },
      { stock: ["alpaca", "alpaca"], option: [] },
      { stock: ["alpaca", "marketdata", "third"], option: [] },
      { stock: ["https://example.com/private-key"], option: [] },
      { stock: ["unknown"], option: [] },
      { stock: ["alpaca"], option: [], apiKey: "private-key" },
    ];
    for (const routes of invalid) await assert.rejects(f.save(routes), hasCode("INVALID_ARGUMENT"));
    for (const request of [
      { expectedRevision: -1, routes: DEFAULT_ROUTES },
      { expectedRevision: 1.5, routes: DEFAULT_ROUTES },
      { expectedRevision: "0", routes: DEFAULT_ROUTES },
      { expectedRevision: 0, routes: DEFAULT_ROUTES, apiKey: "private-key" },
    ]) {
      await assert.rejects(f.service.save({ request, requestId: "invalid", authorize: f.authorize }), hasCode("INVALID_ARGUMENT"));
    }
    const unsupported = new MarketSettingsService({
      database: f.database, writeCoordinator: f.writeCoordinator, marketData: createController().controller,
      providers: PROVIDERS.map((provider) => ({ ...provider, assetTypes: ["stock"] })),
      defaultRoutes: { stock: [], option: [] }, now: f.now,
    });
    await unsupported.initialize();
    await assert.rejects(unsupported.save({
      request: { expectedRevision: 0, routes: { stock: [], option: ["alpaca"] } },
      requestId: "unsupported", authorize: f.authorize,
    }), hasCode("INVALID_ARGUMENT"));
    assert.equal(await loadMarketSettings(f.database), null);
    assert.equal(f.runtime.calls.length, 1);
  });

  it("fails explicitly on corrupt persisted structure and does not overwrite the record", async (t) => {
    const f = await fixture(t);
    await f.save(CHANGED_ROUTES);
    for (const corrupt of [
      { stock: ["alpaca", "alpaca"], option: [] },
      { stock: ["alpaca"], option: [], secret: "private-key" },
      { stock: "alpaca", option: [] },
    ]) {
      await f.writeCoordinator.enqueue(() => f.database.connection.run(`
        UPDATE market_source_settings SET routes_json = CAST($routes AS JSON)
      `, { routes: JSON.stringify(corrupt) }));
      const runtime = createController();
      const restored = new MarketSettingsService({
        database: f.database, writeCoordinator: f.writeCoordinator, marketData: runtime.controller,
        providers: PROVIDERS, defaultRoutes: DEFAULT_ROUTES, now: f.now,
      });
      await assert.rejects(restored.initialize(), /持久记录内容无效/u);
      assert.equal(runtime.calls.length, 0);
      const stored = await f.database.connection.runAndReadAll("SELECT CAST(routes_json AS VARCHAR) AS routes FROM market_source_settings");
      assert.deepEqual(JSON.parse(String(stored.getRowObjectsJson()[0]!.routes)), corrupt);
    }
  });

  it("rolls back a failed commit and leaves active routes and the committed view unchanged", async (t) => {
    const f = await fixture(t);
    const before = f.service.getSettings();
    const run = f.database.connection.run;
    let rejectCommit = true;
    f.database.connection.run = async function (...args: Parameters<typeof run>) {
      if (args[0] === "COMMIT" && rejectCommit) {
        rejectCommit = false;
        throw new Error("simulated commit write failure");
      }
      return run.apply(this, args);
    };
    try {
      await assert.rejects(f.save(CHANGED_ROUTES), /simulated commit write failure/u);
      assert.equal(await loadMarketSettings(f.database), null);
      assert.deepEqual(f.service.getSettings(), before);
      assert.equal(f.runtime.calls.length, 1);
      assert.equal((await f.save(CHANGED_ROUTES)).revision, 1);
    } finally {
      f.database.connection.run = run;
    }
  });
});

async function httpFixture(t: TestContext) {
  const f = await fixture(t);
  const app = buildHttpServer({
    protocolMetadata: createServerProtocolMetadata("0.1.0", CURRENT_SCHEMA_VERSION),
    getHealthSnapshot: () => { throw new Error("health is outside this test"); },
  });
  const authenticateDevice = createDeviceAuthGate(f.database, f.now);
  const deviceService = new DevicePairingService({ database: f.database, writeCoordinator: f.writeCoordinator, now: f.now });
  const publicKeyPem = generateKeyPairSync("ed25519").publicKey.export({ type: "spki", format: "pem" }).toString();
  const credentials = await deviceService.authorizeOwnerDevice({
    identity: { provider: "google", subject: "owner-subject", email: "owner@example.com", displayName: "Owner" },
    displayName: "Settings Android", publicKeyPem,
  });
  registerMarketSettingsRoutes(app, { marketSettingsService: f.service, authenticateDevice });
  t.after(() => app.close());
  const headers = { ...PROTOCOL_HEADERS, authorization: `Bearer ${credentials.accessToken}` };
  return { ...f, app, headers, credentials, authenticateDevice, deviceService };
}

describe("authenticated market settings REST", () => {
  it("returns public choices, saves ordered sources and disables either asset with no-store responses", async (t) => {
    const f = await httpFixture(t);
    const initial = await f.app.inject({ method: "GET", url: "/v1/market/settings", headers: f.headers });
    assert.equal(initial.statusCode, 200, initial.body);
    assert.deepEqual(initial.json(), f.service.getSettings());
    assert.equal(initial.headers["cache-control"], "no-store");
    const saved = await f.app.inject({
      method: "PUT", url: "/v1/market/settings", headers: f.headers,
      payload: { expectedRevision: 0, routes: CHANGED_ROUTES },
    });
    assert.equal(saved.statusCode, 200, saved.body);
    assert.equal(saved.headers["cache-control"], "no-store");
    assert.equal(saved.json().revision, 1);
    assert.deepEqual(saved.json().routes, CHANGED_ROUTES);
    const conflict = await f.app.inject({
      method: "PUT", url: "/v1/market/settings", headers: f.headers,
      payload: { expectedRevision: 0, routes: DEFAULT_ROUTES },
    });
    assert.equal(conflict.statusCode, 409);
    assert.equal(conflict.json().code, "MARKET_SETTINGS_CHANGED");
    assert.equal(conflict.headers["cache-control"], "no-store");
    const disabled = await f.app.inject({
      method: "PUT", url: "/v1/market/settings", headers: f.headers,
      payload: { expectedRevision: 1, routes: { stock: [], option: [] } },
    });
    assert.equal(disabled.statusCode, 200, disabled.body);
    assert.equal(f.runtime.controller.status().enabled, false);
    assert.equal(disabled.body.includes(f.credentials.accessToken), false);
  });

  it("requires a valid device for GET and PUT and rejects additional payload or query fields", async (t) => {
    const f = await httpFixture(t);
    for (const method of ["GET", "PUT"] as const) {
      for (const headers of [PROTOCOL_HEADERS, { ...PROTOCOL_HEADERS, authorization: "Bearer fake" }]) {
        const response = await f.app.inject({
          method, url: "/v1/market/settings", headers,
          ...(method === "PUT" ? { payload: { expectedRevision: 0, routes: CHANGED_ROUTES } } : {}),
        });
        assert.equal(response.statusCode, 401, response.body);
        assert.equal(response.headers["cache-control"], "no-store");
      }
      const query = await f.app.inject({
        method, url: "/v1/market/settings?provider=private-key", headers: f.headers,
        ...(method === "PUT" ? { payload: { expectedRevision: 0, routes: CHANGED_ROUTES } } : {}),
      });
      assert.equal(query.statusCode, 400);
      assert.equal(query.json().code, "INVALID_ARGUMENT");
      assert.equal(query.body.includes("private-key"), false);
    }
    const invalid = await f.app.inject({
      method: "PUT", url: "/v1/market/settings", headers: f.headers,
      payload: { expectedRevision: 0, routes: CHANGED_ROUTES, apiKey: "private-key" },
    });
    assert.equal(invalid.statusCode, 400);
    assert.equal(invalid.json().code, "INVALID_ARGUMENT");
    assert.equal(invalid.body.includes("private-key"), false);
    assert.equal(await loadMarketSettings(f.database), null);
  });

  it("rechecks credentials inside the shared queue after an earlier queued logout", async (t) => {
    const f = await httpFixture(t);
    let release!: () => void;
    const held = f.writeCoordinator.enqueue(() => new Promise<void>((resolve) => { release = resolve; }));
    await waitFor(() => release !== undefined);
    const logout = f.deviceService.revokeDevice(f.credentials.deviceId);
    const put = f.app.inject({
      method: "PUT", url: "/v1/market/settings", headers: f.headers,
      payload: { expectedRevision: 0, routes: CHANGED_ROUTES },
    }).then((response) => response);
    try {
      await waitFor(() => f.writeCoordinator.status().pendingOperations === 3);
      release();
      await held;
      await logout;
      const denied = await put;
      assert.equal(denied.statusCode, 401, denied.body);
      assert.ok(["DEVICE_REVOKED", "DEVICE_CREDENTIAL_INVALID"].includes(denied.json().code));
      assert.equal(await loadMarketSettings(f.database), null);
      assert.deepEqual(f.service.getSettings().routes, DEFAULT_ROUTES);
      assert.equal(f.runtime.calls.length, 1);
    } finally {
      release();
      await held;
      await logout;
      await put;
    }
  });
});

async function waitFor(predicate: () => boolean): Promise<void> {
  const deadline = Date.now() + 5_000;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("expected write queue state was not reached");
    await setImmediate();
  }
}
