import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AgentSessionStateError,
  startAgentTurn,
} from "../src/domain/agent/index.js";
import type { AgentSessionSnapshot } from "../src/domain/agent/index.js";

/** 构造可开始新回合的已持久化会话快照。 */
function session(status: AgentSessionSnapshot["status"] = "idle"): AgentSessionSnapshot {
  return {
    id: "session-1",
    provider: "opencode",
    status,
    accountScope: { kind: "all" },
    ledgerVersion: 21,
    asOf: "2026-08-31T05:00:00Z",
    revision: 4,
    messages: [],
  };
}

describe("Agent turn start plan", () => {
  it("先固化 running 快照和用户消息，再生成连续且无正文的起始事件", () => {
    const plan = startAgentTurn({
      session: session(),
      messageId: "message-user-1",
      content: " 解释当前期权风险 ",
      startedAt: "2026-08-31T05:01:00Z",
      lastEventSequence: 8,
    });

    assert.equal(plan.session.status, "running");
    assert.equal(plan.session.revision, 5);
    assert.equal(plan.session.messages[0], plan.userMessage);
    assert.equal(plan.userMessage.content, "解释当前期权风险");
    assert.deepEqual(plan.adapterInput, {
      messageId: "message-user-1",
      content: "解释当前期权风险",
    });
    assert.equal(plan.event.type, "message.started");
    assert.equal(plan.event.sequence, 9);
    assert.equal("content" in plan.event.payload, false);
    assert.equal(plan.event.payload.ledger_version, 21);
  });

  it("拒绝非 idle 会话、空消息和重复消息 ID", () => {
    assert.throws(
      () => startAgentTurn({
        session: session("running"),
        messageId: "message-1",
        content: "继续",
        startedAt: "2026-08-31T05:01:00Z",
        lastEventSequence: 1,
      }),
      AgentSessionStateError,
    );
    assert.throws(
      () => startAgentTurn({
        session: session(),
        messageId: "message-1",
        content: " ",
        startedAt: "2026-08-31T05:01:00Z",
        lastEventSequence: 1,
      }),
      RangeError,
    );
    const existing = {
      ...session(),
      messages: [{
        id: "message-1",
        role: "user" as const,
        content: "旧消息",
        createdAt: "2026-08-31T05:00:00Z",
      }],
    };
    assert.throws(
      () => startAgentTurn({
        session: existing,
        messageId: "message-1",
        content: "新消息",
        startedAt: "2026-08-31T05:01:00Z",
        lastEventSequence: 1,
      }),
      RangeError,
    );
  });
});
