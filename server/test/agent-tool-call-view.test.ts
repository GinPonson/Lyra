import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createPublicAgentToolCallList } from "../src/application/index.js";
import type { AgentToolCallRecord } from "../src/domain/agent/index.js";

const CALLS: readonly AgentToolCallRecord[] = [
  {
    id: "tool-call-2",
    sessionId: "session-1",
    messageId: "message-2",
    ordinal: 1,
    toolName: "positions",
    authorizationDecision: "denied",
    status: "rejected",
    argumentsSummary: {},
    resultSummary: null,
    ledgerVersion: 18,
    durationMs: 0,
    errorCode: "AGENT_TOOL_FORBIDDEN",
    startedAt: "2026-08-31T15:00:02.000Z",
    completedAt: "2026-08-31T15:00:02.000Z",
  },
  {
    id: "tool-call-1",
    sessionId: "session-1",
    messageId: "message-1",
    ordinal: 1,
    toolName: "summary",
    authorizationDecision: "allowed",
    status: "completed",
    argumentsSummary: { period: "month" },
    resultSummary: { row_count: 1, truncated: false },
    ledgerVersion: 18,
    durationMs: 72,
    errorCode: null,
    startedAt: "2026-08-31T15:00:01.000Z",
    completedAt: "2026-08-31T15:00:01.072Z",
  },
];

describe("public Agent tool call list", () => {
  it("稳定公开安全摘要和终态且隔离内部授权字段", () => {
    const result = createPublicAgentToolCallList(CALLS);

    assert.deepEqual(result.map((call) => call.id), ["tool-call-1", "tool-call-2"]);
    assert.deepEqual(result[0], {
      id: "tool-call-1",
      messageId: "message-1",
      ordinal: 1,
      toolName: "summary",
      status: "completed",
      argumentsSummary: { period: "month" },
      resultSummary: { row_count: 1, truncated: false },
      ledgerVersion: 18,
      durationMs: 72,
      errorCode: null,
      startedAt: "2026-08-31T15:00:01.000Z",
      completedAt: "2026-08-31T15:00:01.072Z",
    });
    const serialized = JSON.stringify(result);
    assert.equal(serialized.includes("authorizationDecision"), false);
    assert.equal(serialized.includes("sessionId"), false);

    (result[0]?.argumentsSummary as Record<string, unknown>).period = "changed";
    assert.deepEqual(CALLS[1]?.argumentsSummary, { period: "month" });
    assert.deepEqual(CALLS.map((call) => call.id), ["tool-call-2", "tool-call-1"]);
  });
});
