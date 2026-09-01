import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AgentMcpCredentialError,
} from "../src/domain/agent/index.js";
import type { AgentSessionSnapshot } from "../src/domain/agent/index.js";
import {
  authenticateAgentMcpCredential,
  issueAgentMcpCredential,
  revokeAgentMcpCredential,
} from "../src/infrastructure/agents/index.js";

function runningSession(): AgentSessionSnapshot {
  return {
    id: "session-1",
    provider: "opencode",
    status: "running",
    accountScope: { kind: "account", account: "TST001" },
    ledgerVersion: 12,
    asOf: "2026-08-31T00:00:00.000Z",
    revision: 4,
    messages: [],
  };
}

describe("Agent MCP session credential", () => {
  it("只持久化哈希，并恢复绑定当前会话数据口径的工具上下文", () => {
    const session = runningSession();
    const issued = issueAgentMcpCredential({
      session,
      now: new Date("2026-08-31T00:00:00.000Z"),
      ttlMilliseconds: 60_000,
    });

    assert.equal(issued.token.length >= 40, true);
    assert.equal("token" in issued.record, false);
    assert.equal(issued.record.tokenHash.includes(issued.token), false);
    assert.deepEqual(issued.record.accountScope, { kind: "account", account: "TST001" });

    const authenticated = authenticateAgentMcpCredential({
      session,
      record: issued.record,
      token: issued.token,
      now: new Date("2026-08-31T00:00:30.000Z"),
    });
    assert.deepEqual(authenticated.context, {
      accountScope: { kind: "account", account: "TST001" },
      ledgerVersion: 12,
      asOf: "2026-08-31T00:00:00.000Z",
    });
    assert.equal(authenticated.record.lastUsedAt, "2026-08-31T00:00:30.000Z");
  });

  it("拒绝错误令牌、切换后的 revision、过期与撤销凭据", () => {
    const session = runningSession();
    const issued = issueAgentMcpCredential({
      session,
      now: new Date("2026-08-31T00:00:00.000Z"),
      ttlMilliseconds: 60_000,
    });
    assert.throws(
      () => authenticateAgentMcpCredential({
        session,
        record: issued.record,
        token: "wrong-token",
        now: new Date("2026-08-31T00:00:10.000Z"),
      }),
      (error: unknown) => (
        error instanceof AgentMcpCredentialError
        && error.internalReason === "invalid_token"
      ),
    );
    assert.throws(
      () => authenticateAgentMcpCredential({
        session: { ...session, revision: 5, provider: "claude" },
        record: issued.record,
        token: issued.token,
        now: new Date("2026-08-31T00:00:10.000Z"),
      }),
      (error: unknown) => (
        error instanceof AgentMcpCredentialError
        && error.internalReason === "session_mismatch"
      ),
    );
    assert.throws(
      () => authenticateAgentMcpCredential({
        session,
        record: issued.record,
        token: issued.token,
        now: new Date("2026-08-31T00:01:00.000Z"),
      }),
      (error: unknown) => (
        error instanceof AgentMcpCredentialError
        && error.internalReason === "expired"
      ),
    );

    const revoked = revokeAgentMcpCredential(
      issued.record,
      session.id,
      new Date("2026-08-31T00:00:20.000Z"),
    );
    assert.equal(revokeAgentMcpCredential(revoked, session.id, new Date()), revoked);
    assert.throws(
      () => authenticateAgentMcpCredential({
        session,
        record: revoked,
        token: issued.token,
        now: new Date("2026-08-31T00:00:30.000Z"),
      }),
      (error: unknown) => (
        error instanceof AgentMcpCredentialError
        && error.internalReason === "revoked"
      ),
    );
  });
});
