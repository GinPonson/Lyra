import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { createPublicGmailConnection } from "../src/application/index.js";
import {
  GMAIL_READONLY_SCOPE,
  createGmailConnectionRecord,
  disconnectGmailConnection,
} from "../src/infrastructure/gmail/index.js";
import { SecretCipher } from "../src/infrastructure/security/index.js";
import type { GmailSyncErrorCode } from "../src/domain/gmail/index.js";

describe("Gmail connection credential", () => {
  it("只持久化绑定连接上下文的 refresh token 密文和已校验 scope", () => {
    const cipher = new SecretCipher(Buffer.alloc(32, 5));
    const connection = createGmailConnectionRecord({
      connectionId: "connection-1",
      identity: {
        subject: "google-subject-1",
        email: " Owner@Example.com ",
        emailVerified: true,
      },
      grant: {
        refreshToken: "sensitive-refresh-token",
        grantedScopeText: `openid ${GMAIL_READONLY_SCOPE} openid`,
      },
      cipher,
      now: new Date("2026-08-31T00:00:00Z"),
    });

    assert.equal(connection.googleEmail, "owner@example.com");
    assert.equal(connection.status, "pending_initial_scan");
    assert.equal(connection.grantedScopes, `${GMAIL_READONLY_SCOPE} openid`);
    assert.equal(connection.encryptedRefreshToken?.includes("sensitive-refresh-token"), false);
    assert.equal(
      cipher.decrypt(
        connection.encryptedRefreshToken ?? "",
        "gmail-connection:connection-1",
      ),
      "sensitive-refresh-token",
    );
    assert.equal("accessToken" in connection, false);
  });

  it("拒绝未验证身份，并在断开时清除 refresh token 且保持幂等", () => {
    const cipher = new SecretCipher(Buffer.alloc(32, 9));
    const create = (emailVerified: boolean) => createGmailConnectionRecord({
      connectionId: "connection-1",
      identity: { subject: "subject", email: "owner@example.com", emailVerified },
      grant: { refreshToken: "refresh-token", grantedScopeText: GMAIL_READONLY_SCOPE },
      cipher,
      now: new Date("2026-08-31T00:00:00Z"),
    });
    assert.throws(() => create(false), RangeError);

    const connected = create(true);
    const disconnected = disconnectGmailConnection(
      connected,
      new Date("2026-08-31T01:00:00Z"),
    );
    assert.equal(disconnected.status, "disconnected");
    assert.equal(disconnected.encryptedRefreshToken, null);
    assert.equal(disconnected.historyId, connected.historyId);
    assert.equal(
      disconnectGmailConnection(disconnected, new Date("2026-09-01T00:00:00Z")),
      disconnected,
    );
  });

  it("公开连接状态但不暴露 Google subject、scope、history 游标或凭据密文", () => {
    const cipher = new SecretCipher(Buffer.alloc(32, 7));
    const connection = createGmailConnectionRecord({
      connectionId: "connection-public",
      identity: {
        subject: "sensitive-google-subject",
        email: "owner@example.com",
        emailVerified: true,
      },
      grant: {
        refreshToken: "sensitive-refresh-token",
        grantedScopeText: GMAIL_READONLY_SCOPE,
      },
      cipher,
      now: new Date("2026-08-31T04:00:00Z"),
    });
    const publicConnection = createPublicGmailConnection({
      ...connection,
      status: "active",
      historyId: "90071992547409930001",
      lastSyncAt: "2026-08-31T05:00:00.000Z",
    });

    assert.deepEqual(publicConnection, {
      id: "connection-public",
      googleEmail: "owner@example.com",
      status: "active",
      lastErrorCode: null,
      lastSyncAt: "2026-08-31T05:00:00.000Z",
      createdAt: "2026-08-31T04:00:00.000Z",
      updatedAt: "2026-08-31T04:00:00.000Z",
    });
    const serialized = JSON.stringify(publicConnection);
    for (const forbidden of [
      "sensitive-google-subject",
      "sensitive-refresh-token",
      "encryptedRefreshToken",
      "grantedScopes",
      "historyId",
      "90071992547409930001",
    ]) {
      assert.equal(serialized.includes(forbidden), false);
    }
    assert.throws(
      () => createPublicGmailConnection({
        ...connection,
        lastErrorCode: "raw Google error body" as GmailSyncErrorCode,
      }),
      RangeError,
    );
  });
});
