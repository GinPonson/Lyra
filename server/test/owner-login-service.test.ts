import assert from "node:assert/strict";
import { generateKeyPairSync, randomBytes } from "node:crypto";
import { describe, it } from "node:test";

import { ApplicationError } from "../src/application/app-error.js";
import { OwnerLoginService, pkceChallenge } from "../src/application/owner-login-service.js";
import {
  LOGIN_PROVIDER_IDS,
  type ConfiguredLoginProvider,
  type LoginIdentity,
  type LoginProviderClient,
  type LoginProviderId,
} from "../src/infrastructure/auth/login-provider.js";

const publicKeyPem = generateKeyPairSync("ed25519").publicKey.export({ type: "spki", format: "pem" }).toString();
const errorCode = (code: string) => (error: unknown): boolean => error instanceof ApplicationError && error.code === code;

function owner(provider: LoginProviderId): LoginIdentity {
  return { provider, subject: provider === "google" ? "google-owner" : "42", email: provider === "google" ? "owner@example.com" : null, displayName: "Owner" };
}

function deferred() {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => { release = resolve; });
  return { promise, release };
}

function fixture(options: {
  configured?: readonly LoginProviderId[];
  exchangeCode?: (provider: LoginProviderId, input: Parameters<LoginProviderClient["exchangeCode"]>[0]) => Promise<LoginIdentity>;
  issue?: () => Promise<void>;
} = {}) {
  let now = new Date("2026-10-11T10:00:00Z");
  const exchanged: { provider: LoginProviderId; input: Parameters<LoginProviderClient["exchangeCode"]>[0] }[] = [];
  const issued: { identity: LoginIdentity; publicKeyPem: string; displayName: string }[] = [];
  const providers: ConfiguredLoginProvider[] = (options.configured ?? LOGIN_PROVIDER_IDS).map((id) => ({
    id,
    name: "configured-name-must-not-leak-secrets",
    client: {
      authorizationUrl: ({ state, nonce, codeChallenge }) => `https://provider.example/${id}?${new URLSearchParams({ state, nonce, code_challenge: codeChallenge })}`,
      exchangeCode: async (input) => {
        exchanged.push({ provider: id, input });
        return options.exchangeCode?.(id, input) ?? owner(id);
      },
    },
    assertOwner: (identity) => {
      if (identity.provider !== id || identity.subject !== owner(id).subject) {
        throw new ApplicationError({ code: "LOGIN_DENIED", message: "此账户无权访问", statusCode: 403, retryable: false });
      }
    },
  }));
  const service = new OwnerLoginService({
    providers,
    now: () => now,
    devicePairingService: {
      authorizeOwnerDevice: async (input) => {
        issued.push(input);
        await options.issue?.();
        return {
          deviceId: "test-device-id", displayName: input.displayName,
          accessToken: "lyra-access", accessTokenExpiresAt: "2026-10-11T10:15:00Z",
          refreshToken: "lyra-refresh", refreshTokenExpiresAt: "2026-11-10T10:00:00Z",
        };
      },
    },
  });
  return {
    service, exchanged, issued,
    advance: (milliseconds: number) => { now = new Date(now.getTime() + milliseconds); },
    start: (provider: LoginProviderId, source?: string) => {
      const verifier = randomBytes(32).toString("base64url");
      const started = service.start(provider, {
        displayName: "My phone", publicKeyPem, codeChallenge: pkceChallenge(verifier), codeChallengeMethod: "S256",
      }, source);
      return { ...started, provider, verifier, state: new URL(started.authorizationUrl).searchParams.get("state")! };
    },
  };
}

describe("Shared owner login transactions", () => {
  it("publishes a fixed three-provider catalog containing availability only and keeps disabled providers closed", () => {
    const f = fixture({ configured: ["github"] });
    assert.deepEqual(f.service.catalog(), { providers: [
      { id: "google", name: "Google", available: false },
      { id: "linuxdo", name: "LINUX DO", available: false },
      { id: "github", name: "GitHub", available: true },
    ] });
    assert.throws(() => f.start("linuxdo"), errorCode("AUTH_NOT_CONFIGURED"));
    assert.equal(f.issued.length, 0);
  });

  it("binds state, login ID and App verifier to their original provider without consuming attempts on mixed routes", async () => {
    const f = fixture({ exchangeCode: async (provider) => ({ ...owner(provider), extraToken: "must-not-reach-app" }) });
    const started = LOGIN_PROVIDER_IDS.map((provider) => f.start(provider));
    for (const attempt of started) {
      assert.equal(attempt.authorizationUrl.includes(attempt.verifier), false);
      assert.notEqual(new URL(attempt.authorizationUrl).searchParams.get("code_challenge"), pkceChallenge(attempt.verifier));
      for (const other of started.filter((candidate) => candidate.provider !== attempt.provider)) {
        await assert.rejects(f.service.completeCallback(other.provider, { state: attempt.state, code: "mixed-provider-code" }), errorCode("LOGIN_FAILED"));
        await assert.rejects(f.service.exchange(other.provider, { loginId: attempt.loginId, codeVerifier: attempt.verifier }), errorCode("LOGIN_FAILED"));
        await assert.rejects(f.service.exchange(attempt.provider, { loginId: attempt.loginId, codeVerifier: other.verifier }), errorCode("LOGIN_FAILED"));
      }
      assert.deepEqual(await f.service.exchange(attempt.provider, { loginId: attempt.loginId, codeVerifier: attempt.verifier }), { status: "pending" });
    }
    assert.equal(f.exchanged.length, 0);
    for (const attempt of started) {
      await f.service.completeCallback(attempt.provider, { state: attempt.state, code: "valid-code" });
      const before = f.issued.length;
      const result = await f.service.exchange(attempt.provider, { loginId: attempt.loginId, codeVerifier: attempt.verifier });
      assert.equal(result.status, "complete");
      if (result.status !== "complete") throw new Error("expected complete login");
      assert.deepEqual(result.user, owner(attempt.provider));
      assert.equal(JSON.stringify(result).includes("must-not-reach-app"), false);
      assert.equal(f.issued.length, before + 1);
      assert.notEqual(f.exchanged.at(-1)?.input.codeVerifier, attempt.verifier);
    }
    assert.equal(f.exchanged.length, 3);
  });

  it("allows one callback and one redemption during concurrent work while leaving other providers independent", async () => {
    const providerReady = deferred();
    const issueReady = deferred();
    const f = fixture({
      exchangeCode: async (provider) => { await providerReady.promise; return owner(provider); },
      issue: () => issueReady.promise,
    });
    const attempt = f.start("github");
    const firstCallback = f.service.completeCallback("github", { state: attempt.state, code: "github-code" });
    await assert.rejects(f.service.completeCallback("github", { state: attempt.state, code: "github-code" }), errorCode("LOGIN_ALREADY_REDEEMED"));
    await assert.rejects(f.service.completeCallback("linuxdo", { state: attempt.state, code: "github-code" }), errorCode("LOGIN_FAILED"));
    assert.deepEqual(await f.service.exchange("github", { loginId: attempt.loginId, codeVerifier: attempt.verifier }), { status: "pending" });
    const independent = f.start("linuxdo");
    assert.deepEqual(await f.service.exchange("linuxdo", { loginId: independent.loginId, codeVerifier: independent.verifier }), { status: "pending" });
    providerReady.release();
    await firstCallback;
    assert.equal(f.issued.length, 0, "callback must not issue App credentials");
    const firstExchange = f.service.exchange("github", { loginId: attempt.loginId, codeVerifier: attempt.verifier });
    await assert.rejects(f.service.exchange("github", { loginId: attempt.loginId, codeVerifier: attempt.verifier }), errorCode("LOGIN_ALREADY_REDEEMED"));
    issueReady.release();
    assert.equal((await firstExchange).status, "complete");
    assert.equal(f.exchanged.length, 1);
    assert.equal(f.issued.length, 1);
  });

  it("shares the rolling source budget across all providers and permits a different source and later window", () => {
    const f = fixture();
    for (let index = 0; index < 20; index += 1) f.start(LOGIN_PROVIDER_IDS[index % 3]!, "same-source");
    for (const provider of LOGIN_PROVIDER_IDS) assert.throws(() => f.start(provider, "same-source"), errorCode("LOGIN_RATE_LIMITED"));
    assert.doesNotThrow(() => f.start("github", "other-source"));
    f.advance(59_999);
    assert.throws(() => f.start("linuxdo", "same-source"), errorCode("LOGIN_RATE_LIMITED"));
    f.advance(1);
    assert.doesNotThrow(() => f.start("linuxdo", "same-source"));
  });

  it("shares capacity across providers, evicts both indexes of the oldest attempt, and keeps the newest usable", async () => {
    const f = fixture();
    const oldest = f.start("google");
    for (let index = 1; index < 128; index += 1) f.start(LOGIN_PROVIDER_IDS[index % 3]!);
    const newest = f.start("linuxdo");
    await assert.rejects(f.service.exchange("google", { loginId: oldest.loginId, codeVerifier: oldest.verifier }), errorCode("LOGIN_EXPIRED"));
    await assert.rejects(f.service.completeCallback("google", { state: oldest.state, code: "old-code" }), errorCode("LOGIN_STATE_INVALID"));
    assert.deepEqual(await f.service.exchange("linuxdo", { loginId: newest.loginId, codeVerifier: newest.verifier }), { status: "pending" });
    assert.equal(f.issued.length, 0);
  });

  it("retains accurate consumed/failed results briefly then frees state and login ID across providers", async () => {
    const f = fixture();
    const consumed = f.start("github");
    await f.service.completeCallback("github", { state: consumed.state, code: "valid-code" });
    await f.service.exchange("github", { loginId: consumed.loginId, codeVerifier: consumed.verifier });
    const cancelled = f.start("linuxdo");
    await assert.rejects(f.service.completeCallback("linuxdo", { state: cancelled.state, error: "access_denied" }), errorCode("LOGIN_DENIED"));
    f.advance(59_999);
    f.start("google");
    await assert.rejects(f.service.exchange("github", { loginId: consumed.loginId, codeVerifier: consumed.verifier }), errorCode("LOGIN_ALREADY_REDEEMED"));
    await assert.rejects(f.service.exchange("linuxdo", { loginId: cancelled.loginId, codeVerifier: cancelled.verifier }), errorCode("LOGIN_DENIED"));
    f.advance(1);
    f.start("google");
    for (const attempt of [consumed, cancelled]) {
      await assert.rejects(f.service.exchange(attempt.provider, { loginId: attempt.loginId, codeVerifier: attempt.verifier }), errorCode("LOGIN_EXPIRED"));
      await assert.rejects(f.service.completeCallback(attempt.provider, { state: attempt.state, code: "old-code" }), errorCode("LOGIN_STATE_INVALID"));
    }
    assert.equal(f.issued.length, 1);
  });

  it("never issues credentials for provider confusion, owner denial, cancellation or expiration during verification", async () => {
    for (const provider of LOGIN_PROVIDER_IDS) {
      for (const identity of [{ ...owner(provider), subject: "different-owner" }, { ...owner(provider), provider: provider === "github" ? "linuxdo" as const : "github" as const }]) {
        const f = fixture({ exchangeCode: async () => identity });
        const attempt = f.start(provider);
        const code = identity.provider === provider ? "LOGIN_DENIED" : "LOGIN_FAILED";
        await assert.rejects(f.service.completeCallback(provider, { state: attempt.state, code: "code" }), errorCode(code));
        await assert.rejects(f.service.exchange(provider, { loginId: attempt.loginId, codeVerifier: attempt.verifier }), errorCode(code));
        assert.equal(f.issued.length, 0);
      }
    }
    const ready = deferred();
    const f = fixture({ exchangeCode: async (provider) => { await ready.promise; return owner(provider); } });
    const attempt = f.start("linuxdo");
    const verification = f.service.completeCallback("linuxdo", { state: attempt.state, code: "valid-but-late-code" });
    f.advance(10 * 60 * 1_000);
    ready.release();
    await assert.rejects(verification, errorCode("LOGIN_EXPIRED"));
    await assert.rejects(f.service.exchange("linuxdo", { loginId: attempt.loginId, codeVerifier: attempt.verifier }), errorCode("LOGIN_EXPIRED"));
    assert.equal(f.issued.length, 0);
  });
});
