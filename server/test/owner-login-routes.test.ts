import assert from "node:assert/strict";
import { generateKeyPairSync, randomBytes } from "node:crypto";
import { describe, it } from "node:test";

import { ApplicationError } from "../src/application/app-error.js";
import { createDeviceAuthGate } from "../src/application/device-auth-gate.js";
import { DevicePairingService, type DevicePairingHttpResponse } from "../src/application/device-pairing-service.js";
import { OwnerLoginService, pkceChallenge } from "../src/application/owner-login-service.js";
import { WriteCoordinator } from "../src/application/write-coordinator.js";
import { LOGIN_PROVIDER_IDS, type LoginIdentity, type LoginProviderId } from "../src/infrastructure/auth/login-provider.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";
import { CURRENT_SCHEMA_VERSION } from "../src/infrastructure/db/schema-version.js";
import { registerDeviceRoutes } from "../src/transport/device-routes.js";
import { buildHttpServer } from "../src/transport/http-server.js";
import { registerLoginRoutes } from "../src/transport/login-routes.js";
import { createServerProtocolMetadata } from "../src/transport/protocol-version.js";

const protocol = { "x-lyra-protocol-version": "2" };
const now = () => new Date("2026-10-11T10:00:00Z");
const publicKey = () => generateKeyPairSync("ed25519").publicKey.export({ type: "spki", format: "pem" }).toString();
const defaultPublicKey = publicKey();
const denied = (error: unknown) => error instanceof ApplicationError && error.statusCode === 401;

function startPayload(verifier: string, publicKeyPem = defaultPublicKey) {
  return { displayName: "Android phone", publicKeyPem, codeChallenge: pkceChallenge(verifier), codeChallengeMethod: "S256" };
}

async function fixture(configured: readonly LoginProviderId[] = LOGIN_PROVIDER_IDS) {
  const database = await openLyraDatabase(":memory:");
  const writeCoordinator = new WriteCoordinator();
  const pairingService = new DevicePairingService({ database, writeCoordinator, now });
  const authenticateDevice = createDeviceAuthGate(database, now);
  const loggedOut: string[] = [];
  let providerError: Error | null = null;
  let ownerSubject = "42";
  const loginService = new OwnerLoginService({
    providers: configured.map((id) => ({
      id, name: "client-secret-and-owner-id-must-not-be-public",
      client: {
        authorizationUrl: ({ state }) => `https://provider.example/${id}?state=${state}`,
        exchangeCode: async () => {
          if (providerError !== null) throw providerError;
          return { provider: id, subject: ownerSubject, email: id === "google" ? "owner@example.com" : null, displayName: "Owner" };
        },
      },
      assertOwner: (identity) => {
        if (identity.provider !== id || identity.subject !== "42") {
          throw new ApplicationError({ code: "LOGIN_DENIED", message: "此账户无权访问", statusCode: 403, retryable: false });
        }
      },
    })),
    devicePairingService: pairingService,
    now,
  });
  const app = buildHttpServer({
    protocolMetadata: createServerProtocolMetadata("0.1.0", CURRENT_SCHEMA_VERSION),
    getHealthSnapshot: async () => { throw new Error("unused health endpoint"); },
  });
  registerDeviceRoutes(app, { pairingService });
  registerLoginRoutes(app, { loginService, pairingService, authenticateDevice, onLogout: (id) => { loggedOut.push(id); } });
  const start = async (provider: LoginProviderId, key = defaultPublicKey) => {
    const verifier = randomBytes(32).toString("base64url");
    const response = await app.inject({ method: "POST", url: `/v1/auth/${provider}/start`, headers: protocol, payload: startPayload(verifier, key) });
    assert.equal(response.statusCode, 201, response.body);
    const body = response.json<{ loginId: string; authorizationUrl: string }>();
    return { provider, loginId: body.loginId, verifier, state: new URL(body.authorizationUrl).searchParams.get("state")! };
  };
  const finish = async (attempt: Awaited<ReturnType<typeof start>>) => {
    const callback = await app.inject({ method: "GET", url: `/v1/auth/${attempt.provider}/callback`, query: { state: attempt.state, code: "one-time-code" } });
    assert.equal(callback.statusCode, 200, callback.body);
    const response = await app.inject({
      method: "POST", url: `/v1/auth/${attempt.provider}/exchange`, headers: protocol,
      payload: { loginId: attempt.loginId, codeVerifier: attempt.verifier },
    });
    assert.equal(response.statusCode, 200, response.body);
    return response.json<DevicePairingHttpResponse & { status: "complete"; user: LoginIdentity }>();
  };
  return {
    app, database, authenticateDevice, loggedOut, start, finish,
    failProvider: (error: Error | null) => { providerError = error; },
    changeOwner: (subject: string) => { ownerSubject = subject; },
    close: async () => { await app.close(); await writeCoordinator.close(); database.close(); },
  };
}

describe("Multi-provider login HTTP routes", () => {
  it("exposes only fixed catalog availability publicly and leaves disabled or anonymous enrollment routes closed", async () => {
    const f = await fixture(["github"]);
    try {
      const catalog = await f.app.inject({ method: "GET", url: "/v1/auth/providers" });
      assert.equal(catalog.statusCode, 200);
      assert.equal(catalog.headers["cache-control"], "no-store");
      assert.deepEqual(catalog.json(), { providers: [
        { id: "google", name: "Google", available: false },
        { id: "linuxdo", name: "LINUX DO", available: false },
        { id: "github", name: "GitHub", available: true },
      ] });
      for (const provider of ["google", "linuxdo"]) {
        const start = await f.app.inject({ method: "POST", url: `/v1/auth/${provider}/start`, headers: protocol, payload: startPayload("v".repeat(43)) });
        assert.equal(start.statusCode, 503);
        assert.equal(start.json().code, "AUTH_NOT_CONFIGURED");
      }
      for (const url of ["/v1/devices/pair", "/v1/devices/pairing-codes", "/v1/auth/unknown/start"]) {
        assert.equal((await f.app.inject({ method: "POST", url, headers: protocol })).statusCode, 404);
      }
    } finally { await f.close(); }
  });

  it("requires protocol headers for start/exchange while allowing browser callbacks for all three routes", async () => {
    const f = await fixture();
    try {
      for (const provider of LOGIN_PROVIDER_IDS) {
        const blockedStart = await f.app.inject({ method: "POST", url: `/v1/auth/${provider}/start`, payload: startPayload("v".repeat(43)) });
        assert.equal(blockedStart.statusCode, 426);
        assert.equal(blockedStart.json().code, "CLIENT_VERSION_INCOMPATIBLE");
        const attempt = await f.start(provider);
        const body = { loginId: attempt.loginId, codeVerifier: attempt.verifier };
        const blockedExchange = await f.app.inject({ method: "POST", url: `/v1/auth/${provider}/exchange`, payload: body });
        assert.equal(blockedExchange.statusCode, 426);
        const pending = await f.app.inject({ method: "POST", url: `/v1/auth/${provider}/exchange`, headers: protocol, payload: body });
        assert.equal(pending.statusCode, 202);
        assert.deepEqual(pending.json(), { status: "pending" });
        const callback = await f.app.inject({ method: "GET", url: `/v1/auth/${provider}/callback`, query: { state: attempt.state, code: "private-oauth-code" } });
        assert.equal(callback.statusCode, 200, callback.body);
        assert.equal(callback.headers["referrer-policy"], "no-referrer");
        assert.equal(callback.headers["cache-control"], "no-store");
        assert.match(String(callback.headers["content-security-policy"]), /default-src 'none'/u);
        assert.equal(callback.headers["x-content-type-options"], "nosniff");
        assert.equal(callback.body.includes(attempt.state), false);
        assert.equal(callback.body.includes("private-oauth-code"), false);
        assert.equal(callback.headers.location, undefined);
        const complete = await f.app.inject({ method: "POST", url: `/v1/auth/${provider}/exchange`, headers: protocol, payload: body });
        assert.equal(complete.statusCode, 200, complete.body);
        assert.equal(complete.json().user.provider, provider);
        assert.equal(complete.json().user.email, provider === "google" ? "owner@example.com" : null);
        assert.equal((await f.authenticateDevice(complete.json().accessToken)).id, complete.json().deviceId);
        assert.equal((await f.app.inject({ method: "POST", url: `/v1/auth/${provider}/exchange`, headers: protocol, payload: body })).statusCode, 409);
      }
    } finally { await f.close(); }
  });

  it("does not consume a valid login through another provider route, forged verifier or body provider override", async () => {
    const f = await fixture();
    try {
      const attempt = await f.start("github");
      const wrongCallback = await f.app.inject({ method: "GET", url: "/v1/auth/linuxdo/callback", query: { state: attempt.state, code: "github-code" } });
      assert.equal(wrongCallback.statusCode, 401);
      assert.equal(wrongCallback.body.includes(attempt.state), false);
      const wrongRoute = await f.app.inject({ method: "POST", url: "/v1/auth/linuxdo/exchange", headers: protocol, payload: { loginId: attempt.loginId, codeVerifier: attempt.verifier } });
      assert.equal(wrongRoute.statusCode, 401);
      const wrongVerifier = await f.app.inject({ method: "POST", url: "/v1/auth/github/exchange", headers: protocol, payload: { loginId: attempt.loginId, codeVerifier: "x".repeat(43) } });
      assert.equal(wrongVerifier.statusCode, 401);
      const override = await f.app.inject({ method: "POST", url: "/v1/auth/github/exchange", headers: protocol, payload: { loginId: attempt.loginId, codeVerifier: attempt.verifier, provider: "linuxdo" } });
      assert.equal(override.statusCode, 400);
      const result = await f.finish(attempt);
      assert.equal(result.user.provider, "github");
      assert.equal((await f.database.connection.runAndReadAll("SELECT count(*)::INTEGER AS count FROM paired_devices")).getRowObjectsJson()[0]?.count, 1);
    } finally { await f.close(); }
  });

  it("returns fixed callback HTML and safe App errors for cancellation, owner denial, malformed callbacks and upstream secrets", async () => {
    const f = await fixture();
    try {
      for (const scenario of ["upstream", "cancelled", "owner"] as const) {
        f.failProvider(scenario === "upstream" ? new Error("raw-upstream-token-secret") : null);
        f.changeOwner(scenario === "owner" ? "43" : "42");
        const attempt = await f.start("linuxdo");
        const callback = await f.app.inject({
          method: "GET", url: "/v1/auth/linuxdo/callback",
          query: { state: attempt.state, code: "private-code-secret", error_description: "raw-upstream-token-secret", ...(scenario === "cancelled" ? { error: "access_denied" } : {}) },
        });
        assert.equal(callback.statusCode, scenario === "upstream" ? 401 : 403);
        const exchange = await f.app.inject({ method: "POST", url: "/v1/auth/linuxdo/exchange", headers: protocol, payload: { loginId: attempt.loginId, codeVerifier: attempt.verifier } });
        assert.equal(exchange.json().code, scenario === "upstream" ? "LOGIN_FAILED" : "LOGIN_DENIED");
        for (const response of [callback, exchange]) {
          for (const secret of [attempt.state, attempt.verifier, "private-code-secret", "raw-upstream-token-secret"]) assert.equal(response.body.includes(secret), false);
        }
      }
      const malformed = await f.app.inject({ method: "GET", url: "/v1/auth/github/callback", query: { code: "<script>secret</script>" } });
      assert.equal(malformed.statusCode, 400);
      assert.equal(malformed.body.includes("<script>"), false);
      assert.equal((await f.database.connection.runAndReadAll("SELECT count(*)::INTEGER AS count FROM device_access_tokens")).getRowObjectsJson()[0]?.count, 0);
    } finally { await f.close(); }
  });

  it("preserves authenticated device-scoped logout across providers and allows safe same-key re-login", async () => {
    const f = await fixture();
    try {
      const firstKey = publicKey();
      const first = await f.finish(await f.start("github", firstKey));
      const second = await f.finish(await f.start("linuxdo", publicKey()));
      assert.notEqual(first.deviceId, second.deviceId);
      const noProtocol = await f.app.inject({ method: "POST", url: "/v1/auth/logout", headers: { authorization: `Bearer ${first.accessToken}` } });
      assert.equal(noProtocol.statusCode, 426);
      assert.equal((await f.app.inject({ method: "POST", url: "/v1/auth/logout", headers: protocol })).statusCode, 401);
      assert.equal((await f.authenticateDevice(first.accessToken)).id, first.deviceId);
      const logout = await f.app.inject({
        method: "POST", url: "/v1/auth/logout", headers: { ...protocol, authorization: `Bearer ${first.accessToken}` },
        payload: { deviceId: second.deviceId },
      });
      assert.equal(logout.statusCode, 204);
      assert.deepEqual(f.loggedOut, [first.deviceId]);
      await assert.rejects(f.authenticateDevice(first.accessToken), denied);
      assert.equal((await f.authenticateDevice(second.accessToken)).id, second.deviceId);
      const revokedRefresh = await f.app.inject({ method: "POST", url: "/v1/devices/tokens", headers: protocol, payload: { refreshToken: first.refreshToken } });
      assert.equal(revokedRefresh.statusCode, 401);
      const relogin = await f.finish(await f.start("google", firstKey));
      assert.equal(relogin.deviceId, first.deviceId);
      assert.equal((await f.authenticateDevice(relogin.accessToken)).id, first.deviceId);
      await assert.rejects(f.authenticateDevice(first.accessToken), denied);
      assert.equal((await f.authenticateDevice(second.accessToken)).id, second.deviceId);
    } finally { await f.close(); }
  });
});
