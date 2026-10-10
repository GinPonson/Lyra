import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AgentSessionStateError,
  transitionAgentSession,
} from "../src/domain/agent/index.js";
import type { AgentSessionSnapshot } from "../src/domain/agent/index.js";

/** 构造尚未运行回合的最小 Agent 会话。 */
function session(): AgentSessionSnapshot {
  return {
    id: "session-1",
    provider: "genkit",
    status: "idle",
    accountScope: { kind: "account", account: "TST276" },
    ledgerVersion: 7,
    asOf: "2026-08-31",
    revision: 1,
    messages: [],
  };
}

describe("Agent session lifecycle", () => {
  it("支持开始、取消、结束和关闭，并让相同状态重放保持幂等", () => {
    const running = transitionAgentSession(session(), "running");
    const cancelling = transitionAgentSession(running, "cancelling");
    const idle = transitionAgentSession(cancelling, "idle");
    const closed = transitionAgentSession(idle, "closed");

    assert.equal(running.revision, 2);
    assert.equal(cancelling.revision, 3);
    assert.equal(idle.revision, 4);
    assert.equal(closed.revision, 5);
    assert.equal(transitionAgentSession(closed, "closed"), closed);
  });

  it("拒绝运行中关闭和终态恢复，且不修改原快照", () => {
    const original = session();
    const running = transitionAgentSession(original, "running");
    for (const [current, target] of [
      [running, "closed"],
      [transitionAgentSession(original, "closed"), "idle"],
    ] as const) {
      assert.throws(
        () => transitionAgentSession(current, target),
        (error: unknown) => error instanceof AgentSessionStateError
          && error.code === "AGENT_SESSION_STATE_CONFLICT",
      );
    }
    assert.equal(original.status, "idle");
    assert.equal(original.revision, 1);
  });
});
