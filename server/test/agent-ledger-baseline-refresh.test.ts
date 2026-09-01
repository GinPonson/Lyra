import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AgentSwitchError,
  prepareAgentLedgerBaselineRefresh,
} from "../src/domain/agent/index.js";
import type { AgentSessionSnapshot } from "../src/domain/agent/index.js";

/** 构造已有账本摘要的完整回合边界会话。 */
function idleSession(): AgentSessionSnapshot {
  return {
    id: "session-baseline-1",
    provider: "claude",
    status: "idle",
    accountScope: { kind: "account", account: "TST001" },
    ledgerVersion: 20,
    asOf: "2026-08-31T05:00:00.000Z",
    revision: 8,
    summary: "版本 20 的持仓与损益摘要",
    messages: [{
      id: "message-baseline-old-1",
      role: "assistant",
      content: "版本 20 的回答",
      createdAt: "2026-08-31T05:01:00.000Z",
    }],
  };
}

describe("Agent ledger baseline refresh", () => {
  it("显式更新同一账户范围的数据基线，并使旧摘要与会话凭据 revision 失效", () => {
    const current = idleSession();
    const plan = prepareAgentLedgerBaselineRefresh({
      session: current,
      ledgerVersion: 23,
      asOf: "2026-08-31T06:00:00.000Z",
      messageId: "message-baseline-refresh-1",
      refreshedAt: "2026-08-31T06:01:00.000Z",
    });

    assert.deepEqual(plan.nextSession.accountScope, { kind: "account", account: "TST001" });
    assert.equal(plan.nextSession.ledgerVersion, 23);
    assert.equal(plan.nextSession.asOf, "2026-08-31T06:00:00.000Z");
    assert.equal(plan.nextSession.revision, 9);
    assert.equal(plan.nextSession.summary, null);
    assert.equal(plan.nextSession.messages.at(-1), plan.contextMessage);
    assert.match(plan.contextMessage.content, /单账户 TST001/);
    assert.match(plan.contextMessage.content, /版本 20.*版本 23/);
    assert.equal(current.ledgerVersion, 20);
    assert.equal(current.summary, "版本 20 的持仓与损益摘要");
  });

  it("拒绝回合中刷新以及不递增的账本版本", () => {
    assert.throws(
      () => prepareAgentLedgerBaselineRefresh({
        session: { ...idleSession(), status: "running" },
        ledgerVersion: 23,
        asOf: "2026-08-31T06:00:00.000Z",
        messageId: "message-baseline-running",
        refreshedAt: "2026-08-31T06:01:00.000Z",
      }),
      (error: unknown) => (
        error instanceof AgentSwitchError
        && error.code === "AGENT_SWITCH_REQUIRES_TURN_BOUNDARY"
      ),
    );
    assert.throws(
      () => prepareAgentLedgerBaselineRefresh({
        session: idleSession(),
        ledgerVersion: 20,
        asOf: "2026-08-31T06:00:00.000Z",
        messageId: "message-baseline-stale",
        refreshedAt: "2026-08-31T06:01:00.000Z",
      }),
      RangeError,
    );
  });
});
