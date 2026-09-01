import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AgentSessionStateError,
  rejectAgentToolCall,
} from "../src/domain/agent/index.js";
import type { AgentSessionSnapshot } from "../src/domain/agent/index.js";

/** 构造允许编排器处理供应商工具请求的会话。 */
function session(status: AgentSessionSnapshot["status"] = "running"): AgentSessionSnapshot {
  return {
    id: "session-2",
    provider: "pi",
    status,
    accountScope: { kind: "account", account: "account-7" },
    ledgerVersion: 52,
    asOf: "2026-08-31T09:00:00Z",
    revision: 14,
    messages: [{
      id: "message-user-7",
      role: "user",
      content: "读取文件并分析",
      createdAt: "2026-08-31T09:01:00Z",
    }],
  };
}

describe("Agent tool policy rejection", () => {
  it("不执行越权工具，并生成无参数记录、统一事件与最小审计事件", () => {
    const plan = rejectAgentToolCall({
      session: session(),
      messageId: "message-user-7",
      callId: "tool-call-7",
      ordinal: 1,
      requestedToolName: "read-file",
      reason: "forbidden_capability",
      rejectedAt: "2026-08-31T09:01:02Z",
      lastEventSequence: 40,
      requestId: "request-7",
    });

    assert.equal(plan.call.authorizationDecision, "denied");
    assert.equal(plan.call.status, "rejected");
    assert.deepEqual(plan.call.argumentsSummary, {});
    assert.equal(plan.call.resultSummary, null);
    assert.equal(plan.call.errorCode, "AGENT_TOOL_FORBIDDEN");
    assert.equal(plan.event.type, "tool.completed");
    assert.equal(plan.event.sequence, 41);
    assert.equal("arguments" in plan.event.payload, false);
    assert.equal(plan.auditEvent.eventType, "agent.tool_rejected");
    assert.equal(plan.auditEvent.outcome, "denied");
    assert.equal(plan.auditEvent.errorCode, "AGENT_TOOL_FORBIDDEN");
    assert.equal("prompt" in plan.auditEvent.metadata, false);
  });

  it("拒绝非 running 会话、未持久化消息和不可审计的工具名", () => {
    const base = {
      messageId: "message-user-7",
      callId: "tool-call-8",
      ordinal: 1,
      requestedToolName: "shell",
      reason: "forbidden_capability" as const,
      rejectedAt: "2026-08-31T09:01:02Z",
      lastEventSequence: 40,
    };
    assert.throws(
      () => rejectAgentToolCall({ ...base, session: session("idle") }),
      AgentSessionStateError,
    );
    assert.throws(
      () => rejectAgentToolCall({
        ...base,
        session: session(),
        messageId: "missing-message",
      }),
      RangeError,
    );
    assert.throws(
      () => rejectAgentToolCall({
        ...base,
        session: session(),
        requestedToolName: "read /etc/passwd",
      }),
      RangeError,
    );
  });
});
