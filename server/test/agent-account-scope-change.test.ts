import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AgentSwitchError,
  prepareAgentAccountScopeChange,
} from "../src/domain/agent/index.js";
import type { AgentSessionSnapshot } from "../src/domain/agent/index.js";

/** 构造已有旧范围摘要的完整回合边界会话。 */
function idleSession(): AgentSessionSnapshot {
  return {
    id: "session-scope-1",
    provider: "genkit",
    status: "idle",
    accountScope: { kind: "account", account: "TST001" },
    ledgerVersion: 12,
    asOf: "2026-08-31T00:00:00.000Z",
    revision: 5,
    summary: "旧账户已实现损益摘要",
    messages: [{
      id: "message-old-1",
      role: "assistant",
      content: "此前回答",
      createdAt: "2026-08-31T00:01:00.000Z",
    }],
  };
}

describe("Agent account scope change", () => {
  it("显式记录新范围和数据基线，并使旧摘要与会话凭据 revision 失效", () => {
    const current = idleSession();
    const plan = prepareAgentAccountScopeChange({
      session: current,
      accountScope: { kind: "all" },
      ledgerVersion: 18,
      asOf: "2026-08-31T03:00:00.000Z",
      messageId: "message-scope-change-1",
      changedAt: "2026-08-31T03:01:00.000Z",
    });

    assert.equal(plan.nextSession.provider, "genkit");
    assert.deepEqual(plan.nextSession.accountScope, { kind: "all" });
    assert.equal(plan.nextSession.ledgerVersion, 18);
    assert.equal(plan.nextSession.revision, 6);
    assert.equal(plan.nextSession.summary, null);
    assert.equal(plan.nextSession.messages.at(-1), plan.contextMessage);
    assert.match(plan.contextMessage.content, /单账户 TST001.*全部活跃账户/);
    assert.match(plan.contextMessage.content, /账本版本 18/);
    assert.deepEqual(current.accountScope, { kind: "account", account: "TST001" });
    assert.equal(current.summary, "旧账户已实现损益摘要");
  });

  it("拒绝回合中变更和无实际变化的账户范围", () => {
    assert.throws(
      () => prepareAgentAccountScopeChange({
        session: { ...idleSession(), status: "running" },
        accountScope: { kind: "all" },
        ledgerVersion: 18,
        asOf: "2026-08-31T03:00:00.000Z",
        messageId: "message-scope-running",
        changedAt: "2026-08-31T03:01:00.000Z",
      }),
      (error: unknown) => (
        error instanceof AgentSwitchError
        && error.code === "AGENT_SWITCH_REQUIRES_TURN_BOUNDARY"
      ),
    );
    assert.throws(
      () => prepareAgentAccountScopeChange({
        session: idleSession(),
        accountScope: { kind: "account", account: "TST001" },
        ledgerVersion: 18,
        asOf: "2026-08-31T03:00:00.000Z",
        messageId: "message-scope-same",
        changedAt: "2026-08-31T03:01:00.000Z",
      }),
      RangeError,
    );
  });
});
