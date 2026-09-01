import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AgentSessionStateError,
  completeAgentTurn,
} from "../src/domain/agent/index.js";
import type { AgentSessionSnapshot } from "../src/domain/agent/index.js";

/** 构造已固化用户消息且供应商正在回答的会话。 */
function runningSession(status: AgentSessionSnapshot["status"] = "running"): AgentSessionSnapshot {
  return {
    id: "session-1",
    provider: "pi",
    status,
    accountScope: { kind: "account", account: "TST276" },
    ledgerVersion: 22,
    asOf: "2026-08-31T06:00:00Z",
    revision: 6,
    messages: [{
      id: "message-user-1",
      role: "user",
      content: "解释风险",
      createdAt: "2026-08-31T06:01:00Z",
    }],
  };
}

describe("Agent turn completion plan", () => {
  it("原子追加完整助手消息、生成无正文完成事件并恢复 idle", () => {
    const plan = completeAgentTurn({
      session: runningSession(),
      messageId: "message-assistant-1",
      content: " 结论来自 risk 工具。 ",
      completedAt: "2026-08-31T06:02:00Z",
      lastEventSequence: 14,
    });

    assert.equal(plan.session.status, "idle");
    assert.equal(plan.session.revision, 7);
    assert.equal(plan.session.messages.at(-1), plan.assistantMessage);
    assert.equal(plan.assistantMessage.content, "结论来自 risk 工具。");
    assert.equal(plan.event.type, "message.completed");
    assert.equal(plan.event.sequence, 15);
    assert.equal("content" in plan.event.payload, false);
    assert.equal(plan.event.payload.ledger_version, 22);
  });

  it("拒绝非 running 会话、空回答和重复助手消息 ID", () => {
    assert.throws(
      () => completeAgentTurn({
        session: runningSession("cancelling"),
        messageId: "message-assistant-1",
        content: "回答",
        completedAt: "2026-08-31T06:02:00Z",
        lastEventSequence: 14,
      }),
      AgentSessionStateError,
    );
    assert.throws(
      () => completeAgentTurn({
        session: runningSession(),
        messageId: "message-assistant-1",
        content: " ",
        completedAt: "2026-08-31T06:02:00Z",
        lastEventSequence: 14,
      }),
      RangeError,
    );
    assert.throws(
      () => completeAgentTurn({
        session: runningSession(),
        messageId: "message-user-1",
        content: "回答",
        completedAt: "2026-08-31T06:02:00Z",
        lastEventSequence: 14,
      }),
      RangeError,
    );
  });
});
