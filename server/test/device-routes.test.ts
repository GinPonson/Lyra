import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import {
  createFinancialToolRegistry,
  DevicePairingService,
  WriteCoordinator,
} from "../src/application/index.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";
import { registerDeviceRoutes } from "../src/transport/device-routes.js";
import { registerLedgerRoutes } from "../src/transport/ledger-routes.js";
import { buildHttpServer, createServerProtocolMetadata } from "../src/transport/index.js";

describe("Device pairing and token issuance routes", () => {
  it("完成配对码创建、设备配对、令牌刷新与重用撤销全流程", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-device-routes-"));
    const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
    try {
      const app = buildHttpServer({
        protocolMetadata: createServerProtocolMetadata("0.1.0", 11),
        getHealthSnapshot: async () => {
          throw new Error("测试未使用健康端点");
        },
      });
      registerDeviceRoutes(app, {
        pairingService: new DevicePairingService({
          database,
          writeCoordinator: new WriteCoordinator(),
        }),
      });
      registerLedgerRoutes(app, {
        registry: createFinancialToolRegistry(database),
        database,
        authenticateDevice: (await import("../src/application/index.js")).createDeviceAuthGate(database),
      });
      const protocol = { "x-lyra-protocol-version": "1" };

      const code = await app.inject({
        method: "POST",
        url: "/v1/devices/pairing-codes",
        headers: protocol,
      });
      assert.equal(code.statusCode, 201);
      const pairingCode = code.json().code as string;
      assert.match(pairingCode, /^[A-Z0-9]{5}-[A-Z0-9]{5}$/u);

      const { publicKey } = generateKeyPairSync("ed25519");
      const publicKeyPem = publicKey.export({ type: "spki", format: "pem" }).toString();

      const badCode = await app.inject({
        method: "POST",
        url: "/v1/devices/pair",
        headers: protocol,
        payload: {
          code: "AAAAA-BBBBB",
          displayName: "Android phone",
          publicKeyPem,
        },
      });
      assert.equal(badCode.statusCode, 400);
      assert.equal(badCode.json().code, "PAIRING_CODE_INVALID");

      const paired = await app.inject({
        method: "POST",
        url: "/v1/devices/pair",
        headers: protocol,
        payload: {
          code: pairingCode.toLowerCase(),
          displayName: "Android phone",
          publicKeyPem,
        },
      });
      assert.equal(paired.statusCode, 201);
      const credentials = paired.json();
      assert.equal(credentials.displayName, "Android phone");
      assert.equal(typeof credentials.accessToken, "string");
      assert.equal(typeof credentials.refreshToken, "string");

      // 配对签发的访问令牌必须能通过业务路由的认证门禁。
      const gated = await app.inject({
        method: "GET",
        url: "/v1/ledger/summary",
        headers: {
          authorization: `Bearer ${credentials.accessToken}`,
          ...protocol,
        },
        query: { scope: "all" },
      });
      assert.equal(gated.statusCode, 200);
      assert.equal(gated.json().metadata.ledgerVersion, 0);

      const replayPair = await app.inject({
        method: "POST",
        url: "/v1/devices/pair",
        headers: protocol,
        payload: {
          code: pairingCode,
          displayName: "Second phone",
          publicKeyPem,
        },
      });
      assert.equal(replayPair.statusCode, 400);

      const refreshed = await app.inject({
        method: "POST",
        url: "/v1/devices/tokens",
        headers: protocol,
        payload: { refreshToken: credentials.refreshToken },
      });
      assert.equal(refreshed.statusCode, 200);
      const newTokens = refreshed.json();
      assert.notEqual(newTokens.refreshToken, credentials.refreshToken);

      const gatedWithNewToken = await app.inject({
        method: "GET",
        url: "/v1/ledger/summary",
        headers: {
          authorization: `Bearer ${newTokens.accessToken}`,
          ...protocol,
        },
        query: { scope: "all" },
      });
      assert.equal(gatedWithNewToken.statusCode, 200);

      // 旧刷新凭据重放必须触发同族撤销。
      const reuse = await app.inject({
        method: "POST",
        url: "/v1/devices/tokens",
        headers: protocol,
        payload: { refreshToken: credentials.refreshToken },
      });
      assert.equal(reuse.statusCode, 401);
      assert.equal(reuse.json().code, "DEVICE_CREDENTIAL_REUSED");

      const revokedFamily = await app.inject({
        method: "POST",
        url: "/v1/devices/tokens",
        headers: protocol,
        payload: { refreshToken: newTokens.refreshToken },
      });
      assert.equal(revokedFamily.statusCode, 401);
      assert.equal(revokedFamily.json().code, "DEVICE_CREDENTIAL_REUSED");

      await app.close();
    } finally {
      await database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
});
