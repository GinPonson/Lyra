import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { describe, it } from "node:test";

import {
  GMAIL_READONLY_SCOPE,
  GmailOAuthError,
  assertGmailReadonlyScope,
  consumeGoogleOAuthTransaction,
  createGoogleOAuthTransaction,
} from "../src/infrastructure/gmail/index.js";

describe("Google OAuth transaction security", () => {
  it("建立绑定设备的高熵 state 与 S256 PKCE 并一次性消费", () => {
    const now = new Date("2026-08-30T00:00:00Z");
    const created = createGoogleOAuthTransaction("device-1", now, 5 * 60 * 1000);
    const expectedChallenge = createHash("sha256")
      .update(created.record.codeVerifier, "ascii")
      .digest("base64url");

    assert.match(created.state, /^[A-Za-z0-9_-]{43}$/);
    assert.equal(created.codeChallenge, expectedChallenge);
    assert.equal(created.record.stateHash.includes(created.state), false);
    const consumed = consumeGoogleOAuthTransaction(
      created.record,
      created.state,
      "device-1",
      now,
    );
    assert.equal(consumed.consumedAt, now.toISOString());
    assert.throws(
      () => consumeGoogleOAuthTransaction(consumed, created.state, "device-1", now),
      (error: unknown) => (
        error instanceof GmailOAuthError
        && error.code === "GOOGLE_AUTH_TRANSACTION_USED"
      ),
    );
  });

  it("拒绝跨设备提交和过期授权事务", () => {
    const now = new Date("2026-08-30T00:00:00Z");
    const created = createGoogleOAuthTransaction("device-1", now, 1_000);
    assert.throws(
      () => consumeGoogleOAuthTransaction(created.record, created.state, "device-2", now),
      (error: unknown) => (
        error instanceof GmailOAuthError
        && error.code === "GOOGLE_AUTH_TRANSACTION_INVALID"
      ),
    );
    assert.throws(
      () => consumeGoogleOAuthTransaction(
        created.record,
        created.state,
        "device-1",
        new Date("2026-08-30T00:00:01Z"),
      ),
      (error: unknown) => (
        error instanceof GmailOAuthError
        && error.code === "GOOGLE_AUTH_TRANSACTION_EXPIRED"
      ),
    );
  });

  it("只接受实际授予 Gmail readonly 的 scope 集合", () => {
    assert.deepEqual(
      assertGmailReadonlyScope(`openid ${GMAIL_READONLY_SCOPE} openid`),
      [GMAIL_READONLY_SCOPE, "openid"],
    );
    assert.throws(
      () => assertGmailReadonlyScope("openid profile"),
      (error: unknown) => (
        error instanceof GmailOAuthError && error.code === "GMAIL_SCOPE_MISSING"
      ),
    );
  });
});
