import assert from "node:assert/strict";
import { generateKeyPairSync, randomBytes } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { ApplicationError } from "../src/application/app-error.js";
import { createDeviceAuthGate } from "../src/application/device-auth-gate.js";
import { DevicePairingService } from "../src/application/device-pairing-service.js";
import { GoogleLoginService, pkceChallenge } from "../src/application/google-login-service.js";
import { WriteCoordinator } from "../src/application/write-coordinator.js";
import type { GoogleLoginIdentity } from "../src/infrastructure/auth/google-login-client.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";
import { registerDeviceRoutes } from "../src/transport/device-routes.js";
import { registerGoogleLoginRoutes } from "../src/transport/google-login-routes.js";
import { buildHttpServer } from "../src/transport/http-server.js";
import { createServerProtocolMetadata } from "../src/transport/protocol-version.js";

const protocol = { "x-lyra-protocol-version": "2" };
const identity: GoogleLoginIdentity = {
  provider: "google", subject: "google-owner-subject", email: "owner@example.com", displayName: "Owner",
};
const publicKeyPem = generateKeyPairSync("ed25519").publicKey.export({ type: "spki", format: "pem" }).toString();
const denied = (error: unknown) => error instanceof ApplicationError && error.statusCode === 401;

async function fixture() {
  const database = await openLyraDatabase(":memory:");
  const writeCoordinator = new WriteCoordinator();
  const pairingService = new DevicePairingService({ database, writeCoordinator });
  const authenticateDevice = createDeviceAuthGate(database);
  const loggedOut: string[] = [];
  let googleIdentity = identity;
  const loginService = new GoogleLoginService({
    config: {
      clientId: "test.apps.googleusercontent.com", clientSecret: "unused-test-secret",
      redirectUri: "https://lyra.example/v1/auth/google/callback", ownerEmail: identity.email, ownerSubject: null,
    },
    googleClient: {
      authorizationUrl: ({ state }) => `https://accounts.google.com/test?state=${state}`,
      exchangeCode: async () => googleIdentity,
    },
    devicePairingService: pairingService,
  });
  const app = buildHttpServer({
    protocolMetadata: createServerProtocolMetadata("0.1.0", 13),
    getHealthSnapshot: async () => { throw new Error("unused health endpoint"); },
  });
  registerDeviceRoutes(app, { pairingService });
  registerGoogleLoginRoutes(app, {
    loginService, pairingService, authenticateDevice,
    onLogout: (deviceId) => { loggedOut.push(deviceId); },
  });
  return {
    app, database, pairingService, authenticateDevice, loggedOut,
    setIdentity: (value: GoogleLoginIdentity) => { googleIdentity = value; },
    start: async () => {
      const verifier = randomBytes(32).toString("base64url");
      const response = await app.inject({
        method: "POST", url: "/v1/auth/google/start", headers: protocol,
        payload: { displayName: "Android phone", publicKeyPem, codeChallenge: pkceChallenge(verifier), codeChallengeMethod: "S256" },
      });
      assert.equal(response.statusCode, 201, response.body);
      const started = response.json();
      return {
        loginId: started.loginId as string,
        state: new URL(started.authorizationUrl as string).searchParams.get("state")!,
        verifier,
      };
    },
    finish: async (started: { loginId: string; state: string; verifier: string }) => {
      const callback = await app.inject({
        method: "GET", url: `/v1/auth/google/callback?${new URLSearchParams({ state: started.state, code: "google-code" })}`,
      });
      assert.equal(callback.statusCode, 200, callback.body);
      assert.equal(callback.headers["cache-control"], "no-store");
      assert.equal(callback.headers["referrer-policy"], "no-referrer");
      assert.equal(callback.body.includes(started.state), false);
      assert.equal(callback.body.includes("google-code"), false);
      return app.inject({
        method: "POST", url: "/v1/auth/google/exchange", headers: protocol,
        payload: { loginId: started.loginId, codeVerifier: started.verifier },
      });
    },
    close: async () => { await app.close(); await writeCoordinator.close(); database.close(); },
  };
}

describe("OAuth device login, refresh and logout routes", () => {
  it("fails closed when OAuth has not been configured", async () => {
    const database = await openLyraDatabase(":memory:");
    const writeCoordinator = new WriteCoordinator();
    const app = buildHttpServer({
      protocolMetadata: createServerProtocolMetadata("0.1.0", 13),
      getHealthSnapshot: async () => { throw new Error("unused"); },
    });
    try {
      const pairingService = new DevicePairingService({ database, writeCoordinator });
      registerDeviceRoutes(app, { pairingService });
      registerGoogleLoginRoutes(app, {
        loginService: null, pairingService, authenticateDevice: createDeviceAuthGate(database),
      });
      const start = await app.inject({
        method: "POST", url: "/v1/auth/google/start", headers: protocol,
        payload: { displayName: "Android", publicKeyPem, codeChallenge: pkceChallenge("a".repeat(43)), codeChallengeMethod: "S256" },
      });
      assert.equal(start.statusCode, 503);
      assert.equal(start.json().code, "AUTH_NOT_CONFIGURED");
      assert.equal((await app.inject({ method: "POST", url: "/v1/devices/pairing-codes", headers: protocol })).statusCode, 404);
      assert.equal((await app.inject({ method: "POST", url: "/v1/auth/logout", headers: protocol })).statusCode, 401);
    } finally {
      await app.close();
      await writeCoordinator.close();
      database.close();
    }
  });

  it("removes anonymous pairing and gates final credentials by App PKCE", async () => {
    const f = await fixture();
    try {
      for (const url of ["/v1/devices/pairing-codes", "/v1/devices/pair"]) {
        assert.equal((await f.app.inject({ method: "POST", url, headers: protocol })).statusCode, 404);
      }
      const started = await f.start();
      const pending = await f.app.inject({
        method: "POST", url: "/v1/auth/google/exchange", headers: protocol,
        payload: { loginId: started.loginId, codeVerifier: started.verifier },
      });
      assert.equal(pending.statusCode, 202);
      assert.deepEqual(pending.json(), { status: "pending" });
      const attacker = await f.app.inject({
        method: "POST", url: "/v1/auth/google/exchange", headers: protocol,
        payload: { loginId: started.loginId, codeVerifier: "a".repeat(43) },
      });
      assert.equal(attacker.statusCode, 401);
      const finished = await f.finish(started);
      assert.equal(finished.statusCode, 200, finished.body);
      const credentials = finished.json();
      assert.equal(credentials.status, "complete");
      assert.deepEqual(credentials.user, identity);
      assert.equal((await f.authenticateDevice(credentials.accessToken)).id, credentials.deviceId);
      const replay = await f.app.inject({
        method: "POST", url: "/v1/auth/google/exchange", headers: protocol,
        payload: { loginId: started.loginId, codeVerifier: started.verifier },
      });
      assert.equal(replay.statusCode, 409);
      const rows = await f.database.connection.runAndReadAll("SELECT count(*) AS count FROM paired_devices");
      assert.equal(Number(rows.getRowObjectsJson()[0]?.count), 1);
    } finally { await f.close(); }
  });

  it("rotates refresh credentials, revokes on logout, and permits same-key re-login", async () => {
    const f = await fixture();
    try {
      const login = await f.finish(await f.start());
      const credentials = login.json();
      const refresh = await f.app.inject({
        method: "POST", url: "/v1/devices/tokens", headers: protocol,
        payload: { refreshToken: credentials.refreshToken },
      });
      assert.equal(refresh.statusCode, 200, refresh.body);
      const tokens = refresh.json();
      assert.notEqual(tokens.refreshToken, credentials.refreshToken);
      assert.equal((await f.authenticateDevice(tokens.accessToken)).id, credentials.deviceId);
      const logout = await f.app.inject({
        method: "POST", url: "/v1/auth/logout", headers: { ...protocol, authorization: `Bearer ${tokens.accessToken}` },
      });
      assert.equal(logout.statusCode, 204, logout.body);
      assert.deepEqual(f.loggedOut, [credentials.deviceId]);
      await assert.rejects(f.authenticateDevice(credentials.accessToken), denied);
      await assert.rejects(f.authenticateDevice(tokens.accessToken), denied);
      const revoked = await f.app.inject({
        method: "POST", url: "/v1/devices/tokens", headers: protocol,
        payload: { refreshToken: tokens.refreshToken },
      });
      assert.equal(revoked.statusCode, 401);
      const relogin = await f.finish(await f.start());
      assert.equal(relogin.statusCode, 200, relogin.body);
      assert.equal(relogin.json().deviceId, credentials.deviceId);
      assert.equal((await f.authenticateDevice(relogin.json().accessToken)).id, credentials.deviceId);
      await assert.rejects(f.authenticateDevice(tokens.accessToken), denied);
    } finally { await f.close(); }
  });

  it("pins the first allowed subject and rejects a different subject with the same email", async () => {
    const f = await fixture();
    try {
      assert.equal((await f.finish(await f.start())).statusCode, 200);
      f.setIdentity({ ...identity, subject: "different-google-account" });
      const rejected = await f.finish(await f.start());
      assert.equal(rejected.statusCode, 403);
      assert.equal(rejected.json().code, "LOGIN_DENIED");
      const reader = await f.database.connection.runAndReadAll("SELECT subject FROM server_owner_identity");
      assert.deepEqual(reader.getRowObjectsJson(), [{ subject: identity.subject }]);
      const devices = await f.database.connection.runAndReadAll("SELECT count(*) AS count FROM paired_devices");
      assert.equal(Number(devices.getRowObjectsJson()[0]?.count), 1);
    } finally { await f.close(); }
  });

  it("rolls back owner enrollment on partial failure and serializes same-key credential issuance", async () => {
    const f = await fixture();
    try {
      await assert.rejects(f.pairingService.authorizeOwnerDevice({ identity, displayName: "", publicKeyPem }));
      const empty = await f.database.connection.runAndReadAll("SELECT count(*) AS count FROM server_owner_identity");
      assert.equal(Number(empty.getRowObjectsJson()[0]?.count), 0);
      const [first, second] = await Promise.all([
        f.pairingService.authorizeOwnerDevice({ identity, displayName: "phone", publicKeyPem }),
        f.pairingService.authorizeOwnerDevice({ identity, displayName: "phone", publicKeyPem }),
      ]);
      assert.equal(first.deviceId, second.deviceId);
      await assert.rejects(f.authenticateDevice(first.accessToken), denied);
      assert.equal((await f.authenticateDevice(second.accessToken)).id, second.deviceId);
      const count = await f.database.connection.runAndReadAll("SELECT count(*) AS count FROM device_refresh_credentials WHERE status = 'active'");
      assert.equal(Number(count.getRowObjectsJson()[0]?.count), 1);
    } finally { await f.close(); }
  });

  it("retains refresh-reuse revocation for all generations in the credential family", async () => {
    const f = await fixture();
    try {
      const original = (await f.finish(await f.start())).json();
      const rotated = await f.pairingService.refreshTokens({ refreshToken: original.refreshToken });
      await assert.rejects(f.pairingService.refreshTokens({ refreshToken: original.refreshToken }), denied);
      await assert.rejects(f.pairingService.refreshTokens({ refreshToken: rotated.refreshToken }), denied);
    } finally { await f.close(); }
  });

  it("invalidates pre-OAuth credentials on upgrade and keeps owner enrollment empty", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-owner-upgrade-"));
    const path = join(directory, "ledger.duckdb");
    let database = await openLyraDatabase(path);
    const writeCoordinator = new WriteCoordinator();
    try {
      const pairing = new DevicePairingService({ database, writeCoordinator });
      const old = await pairing.authorizeOwnerDevice({ identity, displayName: "legacy device", publicKeyPem });
      // Reproduce v12: paired credentials exist but the OAuth owner migration does not.
      await database.connection.run("DROP TABLE server_owner_identity");
      await database.connection.run("DROP TABLE market_source_settings");
      await database.connection.run("DELETE FROM schema_migrations WHERE version >= 13");
      database.close();
      database = await openLyraDatabase(path);
      await assert.rejects(createDeviceAuthGate(database)(old.accessToken), denied);
      const reader = await database.connection.runAndReadAll(`
        SELECT
          (SELECT count(*) FROM server_owner_identity) AS owners,
          (SELECT count(*) FROM paired_devices WHERE status = 'active') AS devices,
          (SELECT count(*) FROM device_access_tokens WHERE status = 'active') AS access,
          (SELECT count(*) FROM device_refresh_credentials WHERE status = 'active') AS refresh
      `);
      const row = reader.getRowObjectsJson()[0]!;
      for (const name of ["owners", "devices", "access", "refresh"]) {
        assert.equal(Number(row[name]), 0, name);
      }
      assert.equal((await database.readHealth()).status, "available");
    } finally {
      await writeCoordinator.close();
      database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
});
