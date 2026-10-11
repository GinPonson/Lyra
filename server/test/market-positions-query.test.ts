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

const DEVICE_ID = "9f000000-0000-4000-8000-000000000001";

/** 建一个带真实设备凭据的 HTTP 服务，用于验证 /v1/market/positions 的查询参数处理。 */
async function fixture(): Promise<{
  app: ReturnType<typeof buildHttpServer>;
  headers: Record<string, string>;
  close: () => Promise<void>;
}> {
  const directory = await mkdtemp(join(tmpdir(), "lyra-market-positions-query-"));
  const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
  await database.connection.run(`
    INSERT INTO paired_devices (
      id, display_name, public_key_pem, public_key_fingerprint, key_algorithm,
      paired_at, updated_at
    ) VALUES (
      CAST($device_id AS UUID), 'Android phone', 'public-key', repeat('d', 64),
      'Ed25519', CAST('2026-09-02T08:00:00Z' AS TIMESTAMPTZ),
      CAST('2026-09-02T08:00:00Z' AS TIMESTAMPTZ)
    )
  `, { device_id: DEVICE_ID });
  const device = (await loadPairedDeviceAuthRecord(database, DEVICE_ID))!;
  const issued = issueDeviceAccessToken(device, new Date(), 60_000);
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
    issued_at: new Date().toISOString(),
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
  return {
    app,
    headers: {
      authorization: `Bearer ${issued.token}`,
      "x-lyra-protocol-version": "2",
    },
    close: async () => {
      await app.close();
      await database.close();
      await rm(directory, { recursive: true, force: true });
    },
  };
}

describe("market positions query parameters", () => {
  it("rejects repeated query parameters instead of silently dropping the filter", async () => {
    const { app, headers, close } = await fixture();
    try {
      // 单个 underlying 正常收窄查询。
      const filtered = await app.inject({
        method: "GET", url: "/v1/market/positions?scope=all&underlying=INTC", headers,
      });
      assert.equal(filtered.statusCode, 200);
      assert.equal(filtered.json().data.basis, "current_positions_at_quote_times_excluding_fees");

      // 重复参数会被解析成数组；必须显式拒绝，绝不能返回未过滤的组合结果。
      const repeated = await app.inject({
        method: "GET", url: "/v1/market/positions?scope=all&underlying=A&underlying=B", headers,
      });
      assert.equal(repeated.statusCode, 400);
      assert.equal(repeated.json().code, "INVALID_ARGUMENT");

      // 未知参数重复出现同样不能静默通过（未知的单个字符串参数本就返回 400）。
      const unknownRepeated = await app.inject({
        method: "GET", url: "/v1/market/positions?scope=all&provider=x&provider=y", headers,
      });
      assert.equal(unknownRepeated.statusCode, 400);
      assert.equal(unknownRepeated.json().code, "INVALID_ARGUMENT");

      // scope 保留既有专用校验：重复 scope 同样以 400 拒绝。
      const repeatedScope = await app.inject({
        method: "GET", url: "/v1/market/positions?scope=all&scope=all", headers,
      });
      assert.equal(repeatedScope.statusCode, 400);
      assert.equal(repeatedScope.json().code, "ACCOUNT_SCOPE_REQUIRED");
    } finally {
      await close();
    }
  });
});
