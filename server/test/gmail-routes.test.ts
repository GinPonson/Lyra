import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import {
  createDeviceAuthGate,
  GmailService,
} from "../src/application/index.js";
import { SecretCipher } from "../src/infrastructure/security/index.js";
import type { GoogleOAuthFetch } from "../src/infrastructure/gmail/google-oauth-client.js";
import { issueDeviceAccessToken } from "../src/infrastructure/security/device-auth.js";
import { loadPairedDeviceAuthRecord } from "../src/infrastructure/db/device-access-token-repository.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";
import { registerGmailRoutes } from "../src/transport/gmail-routes.js";
import { buildHttpServer, createServerProtocolMetadata } from "../src/transport/index.js";

const DEVICE_ID = "a1000000-0000-4000-8000-000000000001";
const ACCOUNT_ID = "a2000000-0000-4000-8000-000000000001";
const OTHER_ACCOUNT_ID = "a3000000-0000-4000-8000-000000000001";
const MASTER_KEY_HEX = "11".repeat(32);

/** 覆盖 token、userinfo 与 revoke 三个 Google 端点的确定性假边界。 */
const fakeGoogleFetch: GoogleOAuthFetch = async (url) => {
  const urlText = String(url instanceof Request ? url.url : url);
  if (urlText.startsWith("https://oauth2.googleapis.com/token")) {
    return new Response(JSON.stringify({
      access_token: "fake-access-token",
      refresh_token: "fake-refresh-token",
      scope: "https://www.googleapis.com/auth/gmail.readonly openid email",
      token_type: "Bearer",
      expires_in: 3_600,
    }), { status: 200, headers: { "content-type": "application/json" } });
  }
  if (urlText.startsWith("https://openidconnect.googleapis.com/v1/userinfo")) {
    return new Response(JSON.stringify({
      sub: "google-sub-1",
      email: "owner@gmail.com",
      email_verified: true,
    }), { status: 200, headers: { "content-type": "application/json" } });
  }
  if (urlText.startsWith("https://oauth2.googleapis.com/revoke")) {
    return new Response("{}", { status: 200 });
  }
  return new Response("unexpected endpoint", { status: 500 });
};

describe("Gmail connection REST routes", () => {
  it("完成授权事务、连接列表、映射改绑与断开撤销", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-gmail-routes-"));
    const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
    try {
      for (const [id, key] of [
        [ACCOUNT_ID, "TST001-1"],
        [OTHER_ACCOUNT_ID, "TST001-2"],
      ] as const) {
        await database.connection.run(`
          INSERT INTO accounts (
            id, display_name, legacy_account_key, masked_suffix, created_at, updated_at
          ) VALUES (
            CAST($account_id AS UUID), 'Primary', $legacy_key, '0001',
            CAST('2026-09-02T08:00:00Z' AS TIMESTAMPTZ),
            CAST('2026-09-02T08:00:00Z' AS TIMESTAMPTZ)
          )
        `, { account_id: id, legacy_key: key });
      }
      await database.connection.run(`
        INSERT INTO paired_devices (
          id, display_name, public_key_pem, public_key_fingerprint, key_algorithm,
          paired_at, updated_at
        ) VALUES (
          CAST($device_id AS UUID), 'Android phone', 'public-key', repeat('f', 64),
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

      const gmailService = new GmailService({
        database,
        cipher: new SecretCipher(Buffer.from(MASTER_KEY_HEX, "hex")),
        googleOAuth: {
          clientId: "fake-client-id",
          redirectUri: "http://127.0.0.1:3141/oauth/callback",
        },
        fetchFn: fakeGoogleFetch,
      });
      const app = buildHttpServer({
        protocolMetadata: createServerProtocolMetadata("0.1.0", 11),
        getHealthSnapshot: async () => {
          throw new Error("测试未使用健康端点");
        },
      });
      registerGmailRoutes(app, {
        gmailService,
        authenticateDevice: createDeviceAuthGate(database),
      });

      const headers = {
        authorization: `Bearer ${issued.token}`,
        "x-lyra-protocol-version": "1",
      };

      const noAuth = await app.inject({
        method: "GET",
        url: "/v1/google/connections",
        headers: { "x-lyra-protocol-version": "1" },
      });
      assert.equal(noAuth.statusCode, 401);

      const created = await app.inject({
        method: "POST",
        url: "/v1/google/authorization-transactions",
        headers,
      });
      assert.equal(created.statusCode, 201);
      const authorizationUrl = created.json().authorizationUrl as string;
      assert.ok(authorizationUrl.includes("client_id=fake-client-id"));
      assert.ok(authorizationUrl.includes("access_type=offline"));
      const state = new URL(authorizationUrl).searchParams.get("state");
      assert.notEqual(state, null);

      const badState = await app.inject({
        method: "POST",
        url: `/v1/google/authorization-transactions/${"z".repeat(43)}/complete`,
        headers,
        payload: { state: "z".repeat(43), code: "some-code" },
      });
      assert.equal(badState.statusCode, 400);
      assert.equal(badState.json().code, "GOOGLE_AUTH_TRANSACTION_INVALID");

      const completed = await app.inject({
        method: "POST",
        url: `/v1/google/authorization-transactions/${state}/complete`,
        headers,
        payload: { state, code: "fake-authorization-code" },
      });
      assert.equal(completed.statusCode, 200);
      const connection = completed.json();
      assert.equal(connection.googleEmail, "owner@gmail.com");
      assert.equal(connection.status, "pending_initial_scan");

      const replayed = await app.inject({
        method: "POST",
        url: `/v1/google/authorization-transactions/${state}/complete`,
        headers,
        payload: { state, code: "fake-authorization-code" },
      });
      assert.equal(replayed.statusCode, 400);
      assert.equal(replayed.json().code, "GOOGLE_AUTH_TRANSACTION_INVALID");

      const connections = await app.inject({
        method: "GET",
        url: "/v1/google/connections",
        headers,
      });
      assert.equal(connections.statusCode, 200);
      assert.equal(connections.json().connections.length, 1);
      assert.equal(connections.json().connections[0].id, connection.id);

      const emptyMappings = await app.inject({
        method: "GET",
        url: `/v1/google/connections/${connection.id}/mappings`,
        headers,
      });
      assert.equal(emptyMappings.statusCode, 200);
      assert.deepEqual(emptyMappings.json().mappings, []);

      await database.connection.run(`
        INSERT INTO gmail_account_mappings (
          id, connection_id, account_suffix, account_id, created_at, updated_at
        ) VALUES (
          CAST($mapping_id AS UUID), CAST($connection_id AS UUID), '0001',
          CAST($account_id AS UUID),
          CAST('2026-09-02T09:00:00Z' AS TIMESTAMPTZ),
          CAST('2026-09-02T09:00:00Z' AS TIMESTAMPTZ)
        )
      `, {
        mapping_id: "b1000000-0000-4000-8000-000000000001",
        connection_id: connection.id,
        account_id: ACCOUNT_ID,
      });

      const suffixMismatch = await app.inject({
        method: "PUT",
        url: `/v1/google/connections/${connection.id}/mappings/b1000000-0000-4000-8000-000000000001`,
        headers,
        payload: { accountId: "b2000000-0000-4000-8000-000000000001" },
      });
      assert.equal(suffixMismatch.statusCode, 409);
      assert.equal(suffixMismatch.json().code, "ACCOUNT_MAPPING_REQUIRED");

      const rebound = await app.inject({
        method: "PUT",
        url: `/v1/google/connections/${connection.id}/mappings/b1000000-0000-4000-8000-000000000001`,
        headers,
        payload: { accountId: OTHER_ACCOUNT_ID },
      });
      assert.equal(rebound.statusCode, 200);
      assert.equal(rebound.json().mapping.accountId, OTHER_ACCOUNT_ID);

      const disconnected = await app.inject({
        method: "DELETE",
        url: `/v1/google/connections/${connection.id}?revoke=true`,
        headers,
      });
      assert.equal(disconnected.statusCode, 200);
      assert.equal(disconnected.json().connection.status, "disconnected");

      const stored = await database.connection.runAndReadAll(`
        SELECT encrypted_refresh_token, status
        FROM gmail_connections
        WHERE id = CAST($id AS UUID)
      `, { id: connection.id });
      assert.equal(stored.getRowObjectsJson()[0]?.encrypted_refresh_token, null);
      assert.equal(stored.getRowObjectsJson()[0]?.status, "disconnected");

      await app.close();
    } finally {
      await database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
});
