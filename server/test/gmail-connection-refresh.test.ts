import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { GmailSyncError } from "../src/domain/gmail/index.js";
import {
  GMAIL_READONLY_SCOPE,
  createGmailConnectionRecord,
  disconnectGmailConnection,
  refreshGmailConnectionAccessToken,
} from "../src/infrastructure/gmail/index.js";
import { SecretCipher } from "../src/infrastructure/security/index.js";

/** 构造一条带加密 refresh token 的 Gmail 连接。 */
function connection(cipher: SecretCipher) {
  return createGmailConnectionRecord({
    connectionId: "connection-1",
    identity: {
      subject: "google-subject-1",
      email: "trader@example.com",
      emailVerified: true,
    },
    grant: {
      refreshToken: "refresh-secret",
      grantedScopeText: `email ${GMAIL_READONLY_SCOPE} openid`,
    },
    cipher,
    now: new Date("2026-08-31T00:00:00Z"),
  });
}

describe("Gmail encrypted connection refresh", () => {
  it("使用连接 ID 作为 AAD 解密并只返回短期 access token", async () => {
    const cipher = new SecretCipher(Buffer.alloc(32, 1));
    const tokenBodies: URLSearchParams[] = [];
    const result = await refreshGmailConnectionAccessToken(
      connection(cipher),
      cipher,
      { clientId: "google-client-id" },
      new Date("2026-08-31T02:00:00Z"),
      async (_input, init) => {
        assert.ok(init?.body instanceof URLSearchParams);
        tokenBodies.push(init.body);
        return new Response(JSON.stringify({
          access_token: "access-secret",
          expires_in: 3600,
          token_type: "Bearer",
        }), { status: 200 });
      },
    );

    assert.equal(tokenBodies[0]?.get("refresh_token"), "refresh-secret");
    assert.equal(result.accessToken, "access-secret");
    assert.equal("refreshToken" in result, false);
  });

  it("已断开连接不请求网络，错误密钥不泄露密文或令牌", async () => {
    const cipher = new SecretCipher(Buffer.alloc(32, 1));
    let fetchCalls = 0;
    const fetchFn = async (): Promise<Response> => {
      fetchCalls += 1;
      return new Response("{}", { status: 200 });
    };
    await assert.rejects(
      refreshGmailConnectionAccessToken(
        disconnectGmailConnection(connection(cipher), new Date("2026-08-31T01:00:00Z")),
        cipher,
        { clientId: "google-client-id" },
        new Date("2026-08-31T02:00:00Z"),
        fetchFn,
      ),
      (error: unknown) => error instanceof GmailSyncError
        && error.code === "GMAIL_AUTH_REVOKED",
    );
    assert.equal(fetchCalls, 0);

    let decryptionFailure: unknown;
    try {
      await refreshGmailConnectionAccessToken(
        connection(cipher),
        new SecretCipher(Buffer.alloc(32, 2)),
        { clientId: "google-client-id" },
        new Date("2026-08-31T02:00:00Z"),
        fetchFn,
      );
    } catch (error) {
      decryptionFailure = error;
    }
    assert.ok(decryptionFailure instanceof GmailSyncError);
    assert.equal(decryptionFailure.code, "GMAIL_CONNECTION_ERROR");
    assert.equal(decryptionFailure.message.includes("refresh-secret"), false);
    assert.equal(decryptionFailure.message.includes("v1."), false);
    assert.equal(fetchCalls, 0);
  });
});
