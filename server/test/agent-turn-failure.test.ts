import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AgentSessionStateError,
  failAgentTurn,
} from "../src/domain/agent/index.js";
import type { AgentSessionSnapshot } from "../src/domain/agent/index.js";

/** 构造供应商正在回答或已经进入取消阶段的会话。 */
function activeSession(status: "running" | "cancelling"): AgentSessionSnapshot {
  return {
    id: "session-1",
    provider: "claude",
    status,
    accountScope: { kind: "all" },
    ledgerVersion: 31,
    asOf: "2026-08-31T07:00:00Z",
    revision: status === "running" ? 8 : 9,
    messages: [{
      id: "message-user-1",
      role: "user",
      content: "分析组合集中度",
      createdAt: "2026-08-31T07:01:00Z",
    }],
  };
}

describe("Agent turn failure plan", () => {
  it("保留明确标记的部分回答，以稳定失败码生成无正文事件并恢复 idle", () => {
    const plan = failAgentTurn({
      session: activeSession("running"),
      messageId: "message-assistant-1",
      outcome: "failed",
      partialContent: " 已完成持仓读取，但风险计算未完成。 ",
      finishedAt: "2026-08-31T07:02:00Z",
      lastEventSequence: 20,
    });

    assert.equal(plan.session.status, "idle");
    assert.equal(plan.session.revision, 9);
    assert.equal(plan.session.messages.at(-1), plan.assistantMessage);
    assert.deepEqual(plan.assistantMessage, {
      id: "message-assistant-1",
      role: "assistant",
      content: "已完成持仓读取，但风险计算未完成。",
      createdAt: "2026-08-31T07:02:00Z",
      status: "failed",
      errorCode: "AGENT_TURN_FAILED",
    });
    assert.equal(plan.event.type, "message.failed");
    assert.equal(plan.event.sequence, 21);
    assert.equal(plan.event.payload.outcome, "failed");
    assert.equal(plan.event.payload.error_code, "AGENT_TURN_FAILED");
    assert.equal("content" in plan.event.payload, false);
  });

  it("取消且无部分回答时不伪造消息，并拒绝不匹配状态与重复消息 ID", () => {
    const plan = failAgentTurn({
      session: activeSession("cancelling"),
      messageId: "message-assistant-1",
      outcome: "cancelled",
      partialContent: "  ",
      finishedAt: "2026-08-31T07:02:00Z",
      lastEventSequence: 21,
    });

    assert.equal(plan.session.status, "idle");
    assert.equal(plan.session.revision, 10);
    assert.equal(plan.assistantMessage, null);
    assert.equal(plan.session.messages.length, 1);
    assert.equal(plan.event.payload.error_code, "AGENT_TURN_CANCELLED");
    assert.throws(
      () => failAgentTurn({
        session: activeSession("running"),
        messageId: "message-assistant-2",
        outcome: "cancelled",
        partialContent: null,
        finishedAt: "2026-08-31T07:02:00Z",
        lastEventSequence: 21,
      }),
      AgentSessionStateError,
    );
    assert.throws(
      () => failAgentTurn({
        session: activeSession("running"),
        messageId: "message-user-1",
        outcome: "failed",
        partialContent: null,
        finishedAt: "2026-08-31T07:02:00Z",
        lastEventSequence: 21,
      }),
      RangeError,
    );
  });
});
