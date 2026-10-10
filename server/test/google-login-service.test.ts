import assert from "node:assert/strict";
import { generateKeyPairSync, randomBytes } from "node:crypto";
import { describe, it } from "node:test";

import { ApplicationError } from "../src/application/app-error.js";
import { GoogleLoginService, pkceChallenge } from "../src/application/google-login-service.js";
import type { GoogleLoginIdentity, GoogleLoginProvider } from "../src/infrastructure/auth/google-login-client.js";

const publicKeyPem = generateKeyPairSync("ed25519").publicKey.export({ type: "spki", format: "pem" }).toString();
const identity: GoogleLoginIdentity = {
  provider: "google", subject: "owner-subject", email: "owner@example.com", displayName: "Owner",
};
const errorCode = (code: string) => (error: unknown): boolean => error instanceof ApplicationError && error.code === code;

function fixture(options: {
  exchangeCode?: GoogleLoginProvider["exchangeCode"];
  issue?: () => Promise<void>;
} = {}) {
  let now = new Date("2026-10-10T13:00:00Z");
  let codeExchanges = 0;
  const issued: unknown[] = [];
  const service = new GoogleLoginService({
    config: {
      clientId: "test.apps.googleusercontent.com", clientSecret: "test-secret",
      redirectUri: "https://lyra.example/v1/auth/google/callback",
      ownerEmail: identity.email, ownerSubject: null,
    },
    googleClient: {
      authorizationUrl: ({ state, nonce, codeChallenge }) => `https://accounts.google.com/test?${new URLSearchParams({ state, nonce, code_challenge: codeChallenge })}`,
      exchangeCode: async (input) => { codeExchanges += 1; return options.exchangeCode?.(input) ?? identity; },
    },
    devicePairingService: {
      authorizeOwnerDevice: async (input) => {
        issued.push(input);
        await options.issue?.();
        return {
          deviceId: "device-id", displayName: input.displayName,
          accessToken: "lyra-access", accessTokenExpiresAt: "2026-10-10T13:15:00Z",
          refreshToken: "lyra-refresh", refreshTokenExpiresAt: "2026-11-09T13:00:00Z",
        };
      },
    },
    now: () => now,
  });
  const start = () => {
    const verifier = randomBytes(32).toString("base64url");
    const attempt = service.start({
      displayName: "My Android", publicKeyPem,
      codeChallenge: pkceChallenge(verifier), codeChallengeMethod: "S256",
    });
    const state = new URL(attempt.authorizationUrl).searchParams.get("state")!;
    return { ...attempt, verifier, state };
  };
  return { service, start, issued, codeExchanges: () => codeExchanges, advance: (milliseconds: number) => { now = new Date(now.getTime() + milliseconds); } };
}

describe("Google login transaction and mobile PKCE", () => {
  it("binds the initial key to a secret held only by the initiating App", async () => {
    const f = fixture();
    const attempt = f.start();
    assert.equal(attempt.authorizationUrl.includes(attempt.verifier), false);
    assert.notEqual(new URL(attempt.authorizationUrl).searchParams.get("code_challenge"), pkceChallenge(attempt.verifier));
    await assert.rejects(f.service.exchange({ loginId: attempt.loginId, codeVerifier: "x".repeat(43) }), errorCode("LOGIN_FAILED"));
    assert.deepEqual(await f.service.exchange({ loginId: attempt.loginId, codeVerifier: attempt.verifier }), { status: "pending" });
    await assert.rejects(f.service.completeCallback({ state: "x".repeat(43), code: "code" }), errorCode("LOGIN_STATE_INVALID"));
    assert.equal(f.codeExchanges(), 0);
    await f.service.completeCallback({ state: attempt.state, code: "google-code" });
    assert.equal(f.issued.length, 0, "browser callback must never mint mobile credentials");
    const complete = await f.service.exchange({ loginId: attempt.loginId, codeVerifier: attempt.verifier });
    assert.equal(complete.status, "complete");
    assert.deepEqual(f.issued[0], { identity, publicKeyPem, displayName: "My Android" });
    await assert.rejects(f.service.exchange({ loginId: attempt.loginId, codeVerifier: attempt.verifier }), errorCode("LOGIN_ALREADY_REDEEMED"));
    assert.equal(f.issued.length, 1);
  });

  it("allows only one concurrent callback and one final redemption", async () => {
    let releaseProvider!: () => void;
    const providerReady = new Promise<void>((resolve) => { releaseProvider = resolve; });
    let releaseIssue!: () => void;
    const issueReady = new Promise<void>((resolve) => { releaseIssue = resolve; });
    const f = fixture({
      exchangeCode: async () => { await providerReady; return identity; },
      issue: () => issueReady,
    });
    const attempt = f.start();
    const firstCallback = f.service.completeCallback({ state: attempt.state, code: "code" });
    await assert.rejects(f.service.completeCallback({ state: attempt.state, code: "code" }), errorCode("LOGIN_ALREADY_REDEEMED"));
    releaseProvider();
    await firstCallback;
    const firstExchange = f.service.exchange({ loginId: attempt.loginId, codeVerifier: attempt.verifier });
    await assert.rejects(f.service.exchange({ loginId: attempt.loginId, codeVerifier: attempt.verifier }), errorCode("LOGIN_ALREADY_REDEEMED"));
    releaseIssue();
    assert.equal((await firstExchange).status, "complete");
    assert.equal(f.codeExchanges(), 1);
    assert.equal(f.issued.length, 1);
  });

  it("denies non-owner identity and provider cancellation without issuing credentials", async () => {
    const f = fixture({ exchangeCode: async () => ({ ...identity, email: "other@example.com" }) });
    const attempt = f.start();
    await assert.rejects(f.service.completeCallback({ state: attempt.state, code: "code" }), errorCode("LOGIN_DENIED"));
    await assert.rejects(f.service.exchange({ loginId: attempt.loginId, codeVerifier: attempt.verifier }), errorCode("LOGIN_DENIED"));
    const cancelled = f.start();
    await assert.rejects(f.service.completeCallback({ state: cancelled.state, error: "access_denied" }), errorCode("LOGIN_DENIED"));
    const failed = f.start();
    await assert.rejects(f.service.completeCallback({ state: failed.state, error: "provider_internal_details" }), errorCode("LOGIN_FAILED"));
    assert.equal(f.issued.length, 0);
  });

  it("expires pending and approved attempts and clears them on subsequent starts", async () => {
    const f = fixture();
    const pending = f.start();
    const approved = f.start();
    await f.service.completeCallback({ state: approved.state, code: "code" });
    f.advance(10 * 60 * 1_000);
    await assert.rejects(f.service.completeCallback({ state: pending.state, code: "code" }), errorCode("LOGIN_EXPIRED"));
    await assert.rejects(f.service.exchange({ loginId: approved.loginId, codeVerifier: approved.verifier }), errorCode("LOGIN_EXPIRED"));
    f.start();
    await assert.rejects(f.service.completeCallback({ state: pending.state, code: "code" }), errorCode("LOGIN_STATE_INVALID"));
    assert.equal(f.issued.length, 0);
  });
});
