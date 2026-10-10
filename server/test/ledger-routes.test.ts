import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import {
  createDeviceAuthGate,
  createFinancialToolRegistry,
} from "../src/application/index.js";
import { issueDeviceAccessToken } from "../src/infrastructure/security/device-auth.js";
import { loadPairedDeviceAuthRecord } from "../src/infrastructure/db/device-access-token-repository.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";
import { registerLedgerRoutes } from "../src/transport/ledger-routes.js";
import { buildHttpServer, createServerProtocolMetadata } from "../src/transport/index.js";

const DEVICE_ID = "9c000000-0000-4000-8000-000000000001";
const UNKNOWN_ACCOUNT = "9d000000-0000-4000-8000-000000000001";

describe("Ledger read REST routes", () => {
  it("以权威语境暴露账本与分析查询并强制显式范围", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-ledger-routes-"));
    const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
    try {
      await database.connection.run(`
        INSERT INTO paired_devices (
          id, display_name, public_key_pem, public_key_fingerprint, key_algorithm,
          paired_at, updated_at
        ) VALUES (
          CAST($device_id AS UUID), 'Android phone', 'public-key', repeat('c', 64),
          'Ed25519', CAST('2026-09-02T08:00:00Z' AS TIMESTAMPTZ),
          CAST('2026-09-02T08:00:00Z' AS TIMESTAMPTZ)
        )
      `, { device_id: DEVICE_ID });
      const device = (await loadPairedDeviceAuthRecord(database, DEVICE_ID))!;
      const issued = issueDeviceAccessToken(device, new Date(), 60_000);
      const issuedAt = new Date().toISOString();
      await database.connection.run(`
        INSERT INTO device_access_tokens (
          id, device_id, token_hash, status, expires_at, issued_at
        ) VALUES (
          CAST($id AS UUID), CAST($device_id AS UUID), $token_hash, 'active',
          CAST($expires_at AS TIMESTAMPTZ), CAST($issued_at AS TIMESTAMPTZ)
        )
      `, {
        id: issued.record.id,
        device_id: DEVICE_ID,
        token_hash: issued.record.tokenHash,
        expires_at: issued.record.expiresAt,
        issued_at: issuedAt,
      });

      const app = buildHttpServer({
        protocolMetadata: createServerProtocolMetadata("0.1.0", 11),
        getHealthSnapshot: async () => {
          throw new Error("测试未使用健康端点");
        },
      });
      registerLedgerRoutes(app, {
        registry: createFinancialToolRegistry(database),
        database,
        authenticateDevice: createDeviceAuthGate(database),
        now: () => new Date("2026-09-02T09:00:00.000Z"),
      });

      const headers = {
        authorization: `Bearer ${issued.token}`,
        "x-lyra-protocol-version": "2",
      };

      const noAuth = await app.inject({
        method: "GET",
        url: "/v1/ledger/summary",
        headers: { "x-lyra-protocol-version": "2" },
      });
      assert.equal(noAuth.statusCode, 401);

      const missingScope = await app.inject({
        method: "GET",
        url: "/v1/ledger/summary",
        headers,
      });
      assert.equal(missingScope.statusCode, 400);
      assert.equal(missingScope.json().code, "ACCOUNT_SCOPE_REQUIRED");

      const summary = await app.inject({
        method: "GET",
        url: "/v1/ledger/summary",
        headers,
        query: { scope: "all" },
      });
      assert.equal(summary.statusCode, 200);
      const summaryBody = summary.json();
      assert.deepEqual(summaryBody.metadata, {
        accountScope: { kind: "all" },
        accountIds: [],
        currencyGroups: [],
        aggregateCurrency: null,
        ledgerVersion: 0,
        asOf: "2026-09-02T09:00:00.000Z",
        fetchedAt: "2026-09-02T09:00:00.000Z",
      });
      assert.deepEqual(summaryBody.data, { aggregateCurrency: null, groups: [] });

      const expiring = await app.inject({
        method: "GET",
        url: "/v1/ledger/expiring",
        headers,
        query: { scope: "all", days: "30" },
      });
      assert.equal(expiring.statusCode, 200);
      // days 查询字符串必须被收窄为整数后进入确定性查询。
      const expiringData = expiring.json().data as Record<string, unknown>;
      assert.equal(expiringData.count, 0);
      assert.equal(expiringData.basis, "historical_premium_not_market_value");

      // accounts 是空输入 schema 工具；路由只强制显式账户范围。
      const accounts = await app.inject({
        method: "GET",
        url: "/v1/ledger/accounts",
        headers,
        query: { scope: "all" },
      });
      assert.equal(accounts.statusCode, 200);
      assert.deepEqual(accounts.json().data, { rows: [], count: 0, currencyGroups: [] });

      const missingDays = await app.inject({
        method: "GET",
        url: "/v1/ledger/expiring",
        headers,
        query: { scope: "all" },
      });
      assert.equal(missingDays.statusCode, 400);
      assert.equal(missingDays.json().code, "INVALID_ARGUMENT");

      const unknownAccount = await app.inject({
        method: "GET",
        url: "/v1/ledger/summary",
        headers,
        query: { scope: UNKNOWN_ACCOUNT },
      });
      assert.equal(unknownAccount.statusCode, 400);
      assert.equal(unknownAccount.json().code, "INVALID_ARGUMENT");

      const trades = await app.inject({
        method: "GET",
        url: "/v1/ledger/trades",
        headers,
        query: { scope: "all", limit: "5" },
      });
      assert.equal(trades.statusCode, 200);
      assert.equal(typeof trades.json().metadata.ledgerVersion, "number");

      const drawdown = await app.inject({
        method: "GET",
        url: "/v1/analytics/drawdown",
        headers,
        query: { scope: "all" },
      });
      assert.equal(drawdown.statusCode, 200);
      assert.equal(drawdown.json().metadata.accountScope.kind, "all");

      await app.close();
    } finally {
      await database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
});
