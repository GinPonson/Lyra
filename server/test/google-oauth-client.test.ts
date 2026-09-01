import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  GMAIL_READONLY_SCOPE,
  GOOGLE_GMAIL_AUTHORIZATION_SCOPES,
  GOOGLE_TOKEN_ENDPOINT,
  GOOGLE_USERINFO_ENDPOINT,
  GmailOAuthError,
  exchangeGoogleAuthorizationCode,
} from "../src/infrastructure/gmail/index.js";
import { serializeApiError } from "../src/transport/index.js";

/** 构造不依赖真实 Google 网络的 JSON 响应。 */
function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("Google OAuth REST client", () => {
  it("以 PKCE 交换离线令牌并读取已验证邮箱身份", async () => {
    const calls: Array<{ url: string; init: RequestInit | undefined }> = [];
    const fetchFn = async (input: string | URL, init?: RequestInit): Promise<Response> => {
      const url = input.toString();
      calls.push({ url, init });
      if (url === GOOGLE_TOKEN_ENDPOINT) {
        return jsonResponse({
          access_token: "access-secret",
          refresh_token: "refresh-secret",
          expires_in: 3600,
          token_type: "Bearer",
          scope: `email ${GMAIL_READONLY_SCOPE} openid`,
        });
      }
      assert.equal(url, GOOGLE_USERINFO_ENDPOINT);
      return jsonResponse({
        sub: "google-subject-1",
        email: "Trader@Example.com",
        email_verified: true,
      });
    };

    const result = await exchangeGoogleAuthorizationCode({
      authorizationCode: "one-time-code",
      codeVerifier: "pkce-verifier",
      clientId: "google-client-id",
      clientSecret: "client-secret",
      redirectUri: "com.lyra.app:/oauth2redirect",
      now: new Date("2026-08-31T00:00:00Z"),
    }, fetchFn);

    assert.deepEqual(GOOGLE_GMAIL_AUTHORIZATION_SCOPES, [
      "openid",
      "email",
      GMAIL_READONLY_SCOPE,
    ]);
    assert.equal(calls.length, 2);
    assert.equal(calls[0]?.url, GOOGLE_TOKEN_ENDPOINT);
    assert.equal(calls[0]?.init?.method, "POST");
    const tokenBody = calls[0]?.init?.body;
    assert.ok(tokenBody instanceof URLSearchParams);
    assert.deepEqual(Object.fromEntries(tokenBody), {
      code: "one-time-code",
      code_verifier: "pkce-verifier",
      client_id: "google-client-id",
      redirect_uri: "com.lyra.app:/oauth2redirect",
      grant_type: "authorization_code",
      client_secret: "client-secret",
    });
    assert.deepEqual(calls[1]?.init?.headers, {
      authorization: "Bearer access-secret",
    });
    assert.deepEqual(result, {
      accessToken: "access-secret",
      accessTokenExpiresAt: "2026-08-31T01:00:00.000Z",
      identity: {
        subject: "google-subject-1",
        email: "trader@example.com",
        emailVerified: true,
      },
      grant: {
        refreshToken: "refresh-secret",
        grantedScopeText: `email ${GMAIL_READONLY_SCOPE} openid`,
      },
    });
    assert.equal("authorizationCode" in result, false);
    assert.equal("clientSecret" in result, false);
  });

  it("明确拒绝上游失败、缺失离线令牌与未验证邮箱且不泄露响应正文", async () => {
    let upstream: unknown;
    try {
      await exchangeGoogleAuthorizationCode({
        authorizationCode: "one-time-code",
        codeVerifier: "pkce-verifier",
        clientId: "google-client-id",
        redirectUri: "com.lyra.app:/oauth2redirect",
        now: new Date("2026-08-31T00:00:00Z"),
      }, async () => jsonResponse({ error: "invalid_grant", secret: "raw-secret" }, 400));
    } catch (error) {
      upstream = error;
    }
    assert.ok(upstream instanceof GmailOAuthError);
    assert.equal(upstream.code, "GOOGLE_TOKEN_EXCHANGE_FAILED");
    assert.equal(upstream.retryable, false);
    const serialized = JSON.stringify(serializeApiError(upstream, "request-google"));
    assert.equal(serialized.includes("raw-secret"), false);
    assert.equal(serialized.includes("invalid_grant"), false);

    await assert.rejects(
      exchangeGoogleAuthorizationCode({
        authorizationCode: "one-time-code",
        codeVerifier: "pkce-verifier",
        clientId: "google-client-id",
        redirectUri: "com.lyra.app:/oauth2redirect",
        now: new Date("2026-08-31T00:00:00Z"),
      }, async (input) => input.toString() === GOOGLE_TOKEN_ENDPOINT
        ? jsonResponse({
            access_token: "access-secret",
            expires_in: 3600,
            token_type: "Bearer",
            scope: `email ${GMAIL_READONLY_SCOPE} openid`,
          })
        : jsonResponse({})),
      (error: unknown) => error instanceof GmailOAuthError
        && error.code === "GOOGLE_TOKEN_EXCHANGE_FAILED",
    );

    await assert.rejects(
      exchangeGoogleAuthorizationCode({
        authorizationCode: "one-time-code",
        codeVerifier: "pkce-verifier",
        clientId: "google-client-id",
        redirectUri: "com.lyra.app:/oauth2redirect",
        now: new Date("2026-08-31T00:00:00Z"),
      }, async (input) => input.toString() === GOOGLE_TOKEN_ENDPOINT
        ? jsonResponse({
            access_token: "access-secret",
            refresh_token: "refresh-secret",
            expires_in: 3600,
            token_type: "Bearer",
            scope: `email ${GMAIL_READONLY_SCOPE} openid`,
          })
        : jsonResponse({
            sub: "google-subject-1",
            email: "trader@example.com",
            email_verified: false,
          })),
      (error: unknown) => error instanceof GmailOAuthError
        && error.code === "GOOGLE_IDENTITY_UNVERIFIED",
    );
  });
});
