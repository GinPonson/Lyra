import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { GmailSyncError } from "../src/domain/gmail/index.js";
import {
  GMAIL_READONLY_SCOPE,
  GOOGLE_TOKEN_ENDPOINT,
  refreshGoogleAccessToken,
} from "../src/infrastructure/gmail/index.js";

/** 构造不依赖真实 Google 网络的 JSON 响应。 */
function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("Google access token refresh", () => {
  it("使用离线 refresh token 获取短期 access token 并沿用已验证 scope", async () => {
    let capturedUrl: string | null = null;
    let capturedInit: RequestInit | undefined;
    const result = await refreshGoogleAccessToken({
      refreshToken: "refresh-secret",
      grantedScopeText: `openid ${GMAIL_READONLY_SCOPE} email`,
      clientId: "google-client-id",
      clientSecret: "client-secret",
      now: new Date("2026-08-31T02:00:00Z"),
    }, async (input, init) => {
      capturedUrl = input.toString();
      capturedInit = init;
      return jsonResponse({
        access_token: "new-access-secret",
        expires_in: 3600,
        token_type: "Bearer",
      });
    });

    assert.equal(capturedUrl, GOOGLE_TOKEN_ENDPOINT);
    const body = capturedInit?.body;
    assert.ok(body instanceof URLSearchParams);
    assert.deepEqual(Object.fromEntries(body), {
      refresh_token: "refresh-secret",
      client_id: "google-client-id",
      grant_type: "refresh_token",
      client_secret: "client-secret",
    });
    assert.deepEqual(result, {
      accessToken: "new-access-secret",
      accessTokenExpiresAt: "2026-08-31T03:00:00.000Z",
      grantedScopeText: `email ${GMAIL_READONLY_SCOPE} openid`,
    });
    assert.equal("refreshToken" in result, false);
  });

  it("把失效 refresh token 映射为重新授权且不读取 Google 错误正文", async () => {
    let failure: unknown;
    try {
      await refreshGoogleAccessToken({
        refreshToken: "refresh-secret",
        grantedScopeText: GMAIL_READONLY_SCOPE,
        clientId: "google-client-id",
        now: new Date("2026-08-31T02:00:00Z"),
      }, async () => jsonResponse({
        error: "invalid_grant",
        error_description: "raw-secret",
      }, 400));
    } catch (error) {
      failure = error;
    }

    assert.ok(failure instanceof GmailSyncError);
    assert.equal(failure.code, "GMAIL_AUTH_REVOKED");
    assert.equal(failure.retryable, false);
    assert.equal(failure.message.includes("raw-secret"), false);
    assert.equal(failure.message.includes("invalid_grant"), false);
  });
});
