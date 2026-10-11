import assert from "node:assert/strict";
import { generateKeyPairSync, randomBytes } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { DuckDBInstance } from "@duckdb/node-api";

import { ApplicationError } from "../src/application/app-error.js";
import { createDeviceAuthGate } from "../src/application/device-auth-gate.js";
import { DevicePairingService } from "../src/application/device-pairing-service.js";
import { OwnerLoginService, pkceChallenge } from "../src/application/owner-login-service.js";
import { WriteCoordinator } from "../src/application/write-coordinator.js";
import { LOGIN_PROVIDER_IDS, type LoginIdentity, type LoginProviderId } from "../src/infrastructure/auth/login-provider.js";
import { LyraDatabase, openLyraDatabase } from "../src/infrastructure/db/database.js";
import { CURRENT_SCHEMA_VERSION } from "../src/infrastructure/db/schema-version.js";

const WHEN = "2026-10-11T10:00:00.000Z";
const now = () => new Date(WHEN);
const ACCOUNT_ID = "f1000000-0000-4000-8000-000000000015";
const PRIOR_MIGRATIONS = [
  "schema.sql", "gmail-schema.sql", "auth-schema.sql", "audit-schema.sql",
  "agent-schema.sql", "access-token-schema.sql", "import-preview-schema.sql",
  "reconciliation-reminder-schema.sql", "transaction-source-view-schema.sql",
  "idempotency-schema.sql", "agent-session-identity-schema.sql", "agent-genkit-schema.sql",
  "owner-login-schema.sql", "market-settings-schema.sql",
];
const FINANCIAL_TABLES = ["accounts", "ledger_versions", "ledger_version_accounts", "transactions", "lots", "v_positions", "market_source_settings"] as const;
const AUTH_TABLES = ["server_owner_identity", "paired_devices", "device_access_tokens", "device_refresh_credentials"] as const;
const publicKey = () => generateKeyPairSync("ed25519").publicKey.export({ type: "spki", format: "pem" }).toString();
const loginDenied = (error: unknown) => error instanceof ApplicationError && error.code === "LOGIN_DENIED";

async function previousDatabase(path: string): Promise<LyraDatabase> {
  const instance = await DuckDBInstance.create(path);
  const database = new LyraDatabase(instance, await instance.connect());
  try {
    for (const name of PRIOR_MIGRATIONS) {
      await database.connection.run(await readFile(new URL(`../src/infrastructure/db/${name}`, import.meta.url), "utf8"));
    }
    return database;
  } catch (error) {
    database.close();
    throw error;
  }
}

async function seedLedger(database: LyraDatabase): Promise<void> {
  await database.connection.run(`
    INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
    VALUES (CAST($id AS UUID), 'Existing brokerage account', 'OWNER-LOGIN-MIGRATION', '0015')
  `, { id: ACCOUNT_ID });
  await database.connection.run("INSERT INTO ledger_versions (reason, source_reference) VALUES ('file_import', 'existing-ledger')");
  await database.connection.run("INSERT INTO ledger_version_accounts VALUES (1, CAST($id AS UUID))", { id: ACCOUNT_ID });
  await database.connection.run(`
    INSERT INTO transactions (
      account_id, record_date, transaction_date, action, raw_symbol, asset_type, underlying,
      quantity, price, fees, amount, source_type, source_reference, source_hash, sequence,
      occurrence, business_hash, transaction_hash, parser_version, ledger_version
    ) VALUES (CAST($id AS UUID), DATE '2026-10-11', DATE '2026-10-11', 'Buy', 'AAPL', 'stock',
      'AAPL', 10, 100, 0, -1000, 'csv', 'preserve.csv', repeat('a', 64), 1, 1,
      repeat('b', 64), repeat('c', 64), 'fixture', 1)
  `, { id: ACCOUNT_ID });
  await database.connection.run(`
    INSERT INTO lots (
      account_id, symbol_key, asset_type, underlying, direction, open_date, open_price,
      open_quantity, remaining_quantity, open_transaction_hash, ledger_version
    ) VALUES (CAST($id AS UUID), 'AAPL', 'stock', 'AAPL', 'long', DATE '2026-10-11', 100, 10, 10, repeat('c', 64), 1)
  `, { id: ACCOUNT_ID });
  await database.connection.run(`
    INSERT INTO market_source_settings (singleton_key, revision, routes_json, updated_at)
    VALUES (1, 3, CAST($routes AS JSON), CAST($now AS TIMESTAMPTZ))
  `, { routes: JSON.stringify({ stock: ["alpaca"], option: ["marketdata"] }), now: WHEN });
}

async function snapshot(database: LyraDatabase, tables: readonly string[]): Promise<Record<string, unknown>> {
  const result: Record<string, unknown> = {};
  for (const table of tables) {
    // Table names come only from the fixed test constants above.
    result[table] = (await database.connection.runAndReadAll(`SELECT * FROM ${table} ORDER BY ALL`)).getRowObjectsJson();
  }
  return result;
}

describe("Owner identity schema 15 migration", () => {
  it("reopens a real populated v14 file without revoking Google sessions or changing ledger/source rows", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-owner-providers-upgrade-"));
    const path = join(directory, "ledger.duckdb");
    let database: LyraDatabase | null = null;
    const coordinator = new WriteCoordinator();
    try {
      database = await previousDatabase(path);
      await seedLedger(database);
      const identity: LoginIdentity = { provider: "google", subject: "existing-google-owner", email: "owner@example.com", displayName: "Existing owner" };
      const oldPairing = new DevicePairingService({ database, writeCoordinator: coordinator, now });
      const existing = await oldPairing.authorizeOwnerDevice({ identity, displayName: "Existing Android", publicKeyPem: publicKey() });
      assert.equal((await createDeviceAuthGate(database, now)(existing.accessToken)).id, existing.deviceId);
      assert.deepEqual(await database.readHealth(), { status: "available", schemaVersion: 14, ledgerVersion: 1 });
      const financialBefore = await snapshot(database, FINANCIAL_TABLES);
      const authBefore = await snapshot(database, AUTH_TABLES);
      database.close();
      database = await openLyraDatabase(path);

      assert.deepEqual(await database.readHealth(), { status: "available", schemaVersion: CURRENT_SCHEMA_VERSION, ledgerVersion: 1 });
      assert.deepEqual(await snapshot(database, FINANCIAL_TABLES), financialBefore);
      assert.deepEqual(await snapshot(database, AUTH_TABLES), authBefore);
      assert.equal((await createDeviceAuthGate(database, now)(existing.accessToken)).id, existing.deviceId);
      const upgradedPairing = new DevicePairingService({ database, writeCoordinator: coordinator, now });
      const refreshed = await upgradedPairing.refreshTokens({ refreshToken: existing.refreshToken });
      assert.equal((await createDeviceAuthGate(database, now)(refreshed.accessToken)).id, existing.deviceId);
      assert.deepEqual(await snapshot(database, FINANCIAL_TABLES), financialBefore);
      const authAfterRefresh = await snapshot(database, AUTH_TABLES);
      database.close();
      database = await openLyraDatabase(path);
      assert.deepEqual(await snapshot(database, AUTH_TABLES), authAfterRefresh);
      assert.deepEqual(await snapshot(database, FINANCIAL_TABLES), financialBefore);
      assert.equal((await createDeviceAuthGate(database, now)(refreshed.accessToken)).id, existing.deviceId);
      const migration = await database.connection.runAndReadAll("SELECT count(*)::INTEGER AS count FROM schema_migrations WHERE version = 15");
      assert.equal(migration.getRowObjectsJson()[0]?.count, 1);
    } finally {
      await coordinator.close();
      database?.close();
      await rm(directory, { recursive: true, force: true });
    }
  });

  it("admits explicitly configured providers to one ledger while pinning each provider's first subject independently", async () => {
    const database = await openLyraDatabase(":memory:");
    const coordinator = new WriteCoordinator();
    try {
      await seedLedger(database);
      const financialBefore = await snapshot(database, FINANCIAL_TABLES);
      const pairing = new DevicePairingService({ database, writeCoordinator: coordinator, now });
      const gate = createDeviceAuthGate(database, now);
      const identities: Record<LoginProviderId, LoginIdentity> = {
        google: { provider: "google", subject: "42", email: "owner@example.com", displayName: "Google owner" },
        github: { provider: "github", subject: "42", email: null, displayName: "GitHub owner" },
        linuxdo: { provider: "linuxdo", subject: "42", email: null, displayName: "LINUX DO owner" },
      };
      const configuredSubjects: Record<LoginProviderId, string> = { google: "42", github: "42", linuxdo: "42" };
      const keys: Record<LoginProviderId, string> = { google: publicKey(), github: publicKey(), linuxdo: publicKey() };
      const service = new OwnerLoginService({
        providers: LOGIN_PROVIDER_IDS.map((id) => ({
          id, name: id,
          client: {
            authorizationUrl: ({ state }) => `https://provider.example/${id}?state=${state}`,
            exchangeCode: async () => identities[id],
          },
          assertOwner: (identity) => {
            if (identity.provider !== id || identity.subject !== configuredSubjects[id]) {
              throw new ApplicationError({ code: "LOGIN_DENIED", message: "此账户无权访问", statusCode: 403, retryable: false });
            }
          },
        })),
        devicePairingService: pairing,
        now,
      });
      const login = async (provider: LoginProviderId) => {
        const verifier = randomBytes(32).toString("base64url");
        const started = service.start(provider, {
          displayName: `${provider} phone`, publicKeyPem: keys[provider], codeChallenge: pkceChallenge(verifier), codeChallengeMethod: "S256",
        });
        await service.completeCallback(provider, { state: new URL(started.authorizationUrl).searchParams.get("state")!, code: "provider-code" });
        const result = await service.exchange(provider, { loginId: started.loginId, codeVerifier: verifier });
        assert.equal(result.status, "complete");
        if (result.status !== "complete") throw new Error("expected completed login");
        return result;
      };
      const google = await login("google");
      const github = await login("github");
      const linuxdo = await login("linuxdo");
      for (const result of [google, github, linuxdo]) {
        assert.equal((await gate(result.accessToken)).id, result.deviceId);
        assert.deepEqual(await snapshot(database, FINANCIAL_TABLES), financialBefore);
      }
      let owners = (await database.connection.runAndReadAll("SELECT provider, subject, email, display_name FROM server_owner_identity ORDER BY provider")).getRowObjectsJson();
      assert.deepEqual(owners, [
        { provider: "github", subject: "42", email: null, display_name: "GitHub owner" },
        { provider: "google", subject: "42", email: "owner@example.com", display_name: "Google owner" },
        { provider: "linuxdo", subject: "42", email: null, display_name: "LINUX DO owner" },
      ]);

      identities.google = { ...identities.google, email: "renamed@example.com", displayName: "Renamed owner" };
      const renamed = await login("google");
      assert.equal(renamed.deviceId, google.deviceId);
      assert.equal((await gate(github.accessToken)).id, github.deviceId);
      assert.equal((await gate(linuxdo.accessToken)).id, linuxdo.deviceId);
      owners = (await database.connection.runAndReadAll("SELECT provider, subject, email, display_name FROM server_owner_identity ORDER BY provider")).getRowObjectsJson();
      assert.deepEqual(owners, [
        { provider: "github", subject: "42", email: null, display_name: "GitHub owner" },
        { provider: "google", subject: "42", email: "renamed@example.com", display_name: "Renamed owner" },
        { provider: "linuxdo", subject: "42", email: null, display_name: "LINUX DO owner" },
      ]);

      // Even a later config change cannot silently rebind an already claimed provider.
      identities.github = { ...identities.github, subject: "43" };
      configuredSubjects.github = "43";
      const authBeforeRejected = await snapshot(database, AUTH_TABLES);
      await assert.rejects(login("github"), loginDenied);
      assert.deepEqual(await snapshot(database, AUTH_TABLES), authBeforeRejected);
      assert.equal((await gate(github.accessToken)).id, github.deviceId);
      assert.equal((await gate(linuxdo.accessToken)).id, linuxdo.deviceId);
      assert.deepEqual(await snapshot(database, FINANCIAL_TABLES), financialBefore);
      assert.deepEqual(await database.readHealth(), { status: "available", schemaVersion: CURRENT_SCHEMA_VERSION, ledgerVersion: 1 });
    } finally {
      await coordinator.close();
      database.close();
    }
  });
});
