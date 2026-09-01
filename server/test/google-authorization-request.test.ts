import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  GMAIL_READONLY_SCOPE,
  GOOGLE_AUTHORIZATION_ENDPOINT,
  buildGoogleAuthorizationRequest,
  createGoogleOAuthTransaction,
} from "../src/infrastructure/gmail/index.js";

describe("Google system authorization request", () => {
  it("构造 Android 系统浏览器使用的离线授权 URL 且不暴露 PKCE verifier", () => {
    const transaction = createGoogleOAuthTransaction(
      "device-1",
      new Date("2026-08-31T00:00:00Z"),
      5 * 60 * 1000,
    );
    const request = buildGoogleAuthorizationRequest(
      transaction,
      "google-client-id",
      "com.lyra.app:/oauth2redirect",
    );
    const url = new URL(request.authorizationUrl);

    assert.equal(url.origin + url.pathname, GOOGLE_AUTHORIZATION_ENDPOINT);
    assert.equal(url.searchParams.get("client_id"), "google-client-id");
    assert.equal(url.searchParams.get("redirect_uri"), "com.lyra.app:/oauth2redirect");
    assert.equal(url.searchParams.get("response_type"), "code");
    assert.equal(url.searchParams.get("scope"), `openid email ${GMAIL_READONLY_SCOPE}`);
    assert.equal(url.searchParams.get("state"), transaction.state);
    assert.equal(url.searchParams.get("code_challenge"), transaction.codeChallenge);
    assert.equal(url.searchParams.get("code_challenge_method"), "S256");
    assert.equal(url.searchParams.get("access_type"), "offline");
    assert.equal(url.searchParams.get("prompt"), "consent");
    assert.equal(url.searchParams.get("include_granted_scopes"), "true");
    assert.equal(request.expiresAt, transaction.record.expiresAt);
    assert.equal(request.authorizationUrl.includes(transaction.record.codeVerifier), false);
    assert.equal("record" in request, false);
  });

  it("拒绝非法 redirect URI、state 与 PKCE challenge", () => {
    const transaction = createGoogleOAuthTransaction(
      "device-1",
      new Date("2026-08-31T00:00:00Z"),
      5 * 60 * 1000,
    );
    assert.throws(
      () => buildGoogleAuthorizationRequest(
        transaction,
        "google-client-id",
        "https://user:password@example.com/callback#fragment",
      ),
      RangeError,
    );
    assert.throws(
      () => buildGoogleAuthorizationRequest(
        { ...transaction, state: "short" },
        "google-client-id",
        "com.lyra.app:/oauth2redirect",
      ),
      RangeError,
    );
    assert.throws(
      () => buildGoogleAuthorizationRequest(
        { ...transaction, codeChallenge: "short" },
        "google-client-id",
        "com.lyra.app:/oauth2redirect",
      ),
      RangeError,
    );
  });
});
