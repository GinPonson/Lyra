import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { GmailSyncError } from "../src/domain/gmail/index.js";
import {
  GMAIL_READONLY_SCOPE,
  GOOGLE_TOKEN_REVOCATION_ENDPOINT,
  createGmailConnectionRecord,
  revokeGmailConnectionAuthorization,
} from "../src/infrastructure/gmail/index.js";
import { SecretCipher } from "../src/infrastructure/security/index.js";

/** 构造一条可请求远端撤销的加密 Gmail 连接。 */
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
      grantedScopeText: GMAIL_READONLY_SCOPE,
    },
    cipher,
    now: new Date("2026-08-31T00:00:00Z"),
  });
}

describe("Gmail remote authorization revocation", () => {
  it("Google 确认撤销后才清除本地密文并进入 disconnected", async () => {
    const cipher = new SecretCipher(Buffer.alloc(32, 1));
    const requestBodies: URLSearchParams[] = [];
    const revoked = await revokeGmailConnectionAuthorization(
      connection(cipher),
      cipher,
      new Date("2026-08-31T02:00:00Z"),
      async (input, init) => {
        assert.equal(input.toString(), GOOGLE_TOKEN_REVOCATION_ENDPOINT);
        assert.ok(init?.body instanceof URLSearchParams);
        requestBodies.push(init.body);
        return new Response(null, { status: 200 });
      },
    );

    assert.equal(requestBodies[0]?.get("token"), "refresh-secret");
    assert.equal(revoked.status, "disconnected");
    assert.equal(revoked.encryptedRefreshToken, null);
    assert.equal(revoked.updatedAt, "2026-08-31T02:00:00.000Z");
  });

  it("远端失败时明确报错且保留原连接用于重试", async () => {
    const cipher = new SecretCipher(Buffer.alloc(32, 1));
    const original = connection(cipher);
    let failure: unknown;
    try {
      await revokeGmailConnectionAuthorization(
        original,
        cipher,
        new Date("2026-08-31T02:00:00Z"),
        async () => new Response(JSON.stringify({
          error: "temporarily_unavailable",
          secret: "raw-secret",
        }), { status: 503 }),
      );
    } catch (error) {
      failure = error;
    }

    assert.ok(failure instanceof GmailSyncError);
    assert.equal(failure.code, "GMAIL_CONNECTION_ERROR");
    assert.equal(failure.retryable, true);
    assert.equal(failure.message.includes("raw-secret"), false);
    assert.notEqual(original.encryptedRefreshToken, null);
    assert.equal(original.status, "pending_initial_scan");
  });
});
