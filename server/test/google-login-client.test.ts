import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { ApplicationError } from "../src/application/app-error.js";
import {
  GoogleLoginClient,
  loadGoogleLoginConfig,
  sha256,
  validateGoogleLoginClaims,
  type GoogleLoginConfig,
} from "../src/infrastructure/auth/google-login-client.js";

const config: GoogleLoginConfig = {
  clientId: "lyra-test.apps.googleusercontent.com",
  clientSecret: "test-client-secret-not-real",
  redirectUri: "https://lyra.example/v1/auth/google/callback",
  ownerEmail: "owner@example.com",
  ownerSubject: null,
};
const now = new Date("2026-10-10T13:00:00Z");
const nonce = "nonce-held-by-server";

function validClaims(): Record<string, unknown> {
  return {
    iss: "https://accounts.google.com",
    aud: config.clientId,
    sub: "google-owner-123",
    email: "Owner@Example.com",
    email_verified: true,
    iat: now.getTime() / 1_000,
    exp: now.getTime() / 1_000 + 3_600,
    nonce,
    name: "Owner",
  };
}

const failed = (error: unknown): boolean => error instanceof ApplicationError && error.code === "LOGIN_FAILED";

describe("Google login OIDC boundary", () => {
  it("requires an explicit private owner and exact registered HTTPS callback", () => {
    assert.equal(loadGoogleLoginConfig({}), null);
    assert.throws(() => loadGoogleLoginConfig({ LYRA_LOGIN_GOOGLE_CLIENT_ID: "configured-partially" }));
    const environment = {
      LYRA_LOGIN_GOOGLE_CLIENT_ID: config.clientId,
      LYRA_LOGIN_GOOGLE_CLIENT_SECRET: config.clientSecret,
      LYRA_LOGIN_GOOGLE_REDIRECT_URI: config.redirectUri,
      LYRA_OWNER_EMAIL: " Owner@Example.com ",
    };
    assert.deepEqual(loadGoogleLoginConfig(environment), config);
    assert.throws(() => loadGoogleLoginConfig({ ...environment, LYRA_OWNER_EMAIL: "" }), /owner|OWNER/u);
    for (const redirect of [
      "http://lyra.example/v1/auth/google/callback",
      "https://attacker@lyra.example/v1/auth/google/callback",
      "https://lyra.example/v1/auth/google/callback?next=https://attacker.example",
      "https://lyra.example/other-callback",
    ]) {
      assert.throws(() => loadGoogleLoginConfig({ ...environment, LYRA_LOGIN_GOOGLE_REDIRECT_URI: redirect }));
    }
    assert.equal(loadGoogleLoginConfig({
      ...environment,
      LYRA_LOGIN_GOOGLE_REDIRECT_URI: "http://localhost:3000/v1/auth/google/callback",
    })?.redirectUri, "http://localhost:3000/v1/auth/google/callback");
  });

  it("requests login scopes only and sends server PKCE to Google, never to the App", async () => {
    let form: URLSearchParams | null = null;
    let verifiedToken: string | null = null;
    const client = new GoogleLoginClient(config, {
      fetch: async (url, init) => {
        assert.equal(url, "https://oauth2.googleapis.com/token");
        form = init?.body as URLSearchParams;
        return Response.json({ id_token: "signed-id-token", access_token: "ignored-google-access-token" });
      },
      verifyIdToken: async (token) => { verifiedToken = token; return validClaims(); },
    });
    const authorization = new URL(client.authorizationUrl({ state: "state", nonce, codeChallenge: "server-challenge" }));
    assert.equal(authorization.searchParams.get("scope"), "openid email profile");
    assert.equal(authorization.searchParams.get("redirect_uri"), config.redirectUri);
    assert.equal(authorization.searchParams.get("code_challenge_method"), "S256");
    assert.equal(authorization.searchParams.get("nonce"), nonce);
    assert.equal(authorization.searchParams.has("access_type"), false);
    assert.equal(authorization.searchParams.has("include_granted_scopes"), false);
    assert.equal(authorization.toString().includes(config.clientSecret), false);
    const identity = await client.exchangeCode({
      code: "one-time-google-code", codeVerifier: "server-verifier", nonceHash: sha256(nonce), now,
    });
    assert.equal(verifiedToken, "signed-id-token");
    assert.equal((form as unknown as URLSearchParams).get("code_verifier"), "server-verifier");
    assert.equal((form as unknown as URLSearchParams).get("client_secret"), config.clientSecret);
    assert.deepEqual(identity, { provider: "google", subject: "google-owner-123", email: "owner@example.com", displayName: "Owner" });
    assert.equal(JSON.stringify(identity).includes("token"), false);
  });

  it("rejects invalid nonce, issuer, audience, token time, subject and unverified email", () => {
    const expected = { audience: config.clientId, nonceHash: sha256(nonce), now };
    for (const invalid of [
      { nonce: "other-login" },
      { iss: "https://attacker.example" },
      { aud: "another-client" },
      { azp: "another-client" },
      { exp: now.getTime() / 1_000 },
      { iat: now.getTime() / 1_000 + 3_600 },
      { sub: "" },
      { email_verified: false },
      { email_verified: "true" },
      { email: "not-an-email" },
    ]) {
      assert.throws(() => validateGoogleLoginClaims({ ...validClaims(), ...invalid }, expected), failed);
    }
  });

  it("never accepts failed signature verification or exposes Google errors/tokens", async () => {
    const client = new GoogleLoginClient(config, {
      fetch: async () => Response.json({ id_token: "forged-id-token" }),
      verifyIdToken: async () => { throw new Error("bad signature: private-token-in-provider-error"); },
    });
    await assert.rejects(client.exchangeCode({
      code: "google-code", codeVerifier: "server-verifier", nonceHash: sha256(nonce), now,
    }), (error: unknown) => {
      assert.ok(error instanceof ApplicationError);
      assert.equal(error.code, "LOGIN_FAILED");
      assert.equal(error.cause, undefined);
      assert.equal(error.message.includes("private-token"), false);
      return true;
    });
    const endpointFailure = new GoogleLoginClient(config, {
      fetch: async () => new Response("token=do-not-return", { status: 400 }),
      verifyIdToken: async () => { throw new Error("must not be called"); },
    });
    await assert.rejects(endpointFailure.exchangeCode({
      code: "google-code", codeVerifier: "server-verifier", nonceHash: sha256(nonce), now,
    }), failed);
  });
});
