import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AgentToolPolicyError,
  completeAgentToolCall,
  startAgentToolCall,
} from "../src/domain/agent/index.js";
import type { AgentSessionSnapshot } from "../src/domain/agent/index.js";

/** 构造已经固化用户消息、允许执行只读工具的 running 会话。 */
function runningSession(): AgentSessionSnapshot {
  return {
    id: "session-1",
    provider: "genkit",
    status: "running",
    accountScope: { kind: "account", account: "TST901" },
    ledgerVersion: 41,
    asOf: "2026-08-31T08:00:00Z",
    revision: 11,
    messages: [{
      id: "message-user-1",
      role: "user",
      content: "列出本月已实现收益",
      createdAt: "2026-08-31T08:01:00Z",
    }],
  };
}

describe("Agent tool call lifecycle", () => {
  it("生成绑定服务端账本版本的 started 记录，并以安全摘要完成调用", () => {
    const started = startAgentToolCall({
      session: runningSession(),
      messageId: "message-user-1",
      callId: "tool-call-1",
      ordinal: 1,
      toolName: "realized",
      argumentsSummary: { date_from: "2026-08-01", date_to: "2026-08-31" },
      startedAt: "2026-08-31T08:01:01Z",
      lastEventSequence: 30,
    });
    const completed = completeAgentToolCall({
      call: started.call,
      outcome: "completed",
      resultSummary: { row_count: 12, truncated: false },
      durationMs: 84,
      completedAt: "2026-08-31T08:01:02Z",
      lastEventSequence: started.event.sequence,
    });

    assert.equal(started.call.ledgerVersion, 41);
    assert.equal(started.call.authorizationDecision, "allowed");
    assert.equal(started.event.type, "tool.started");
    assert.equal("arguments" in started.event.payload, false);
    assert.equal(completed.call.status, "completed");
    assert.deepEqual(completed.call.resultSummary, { row_count: 12, truncated: false });
    assert.equal(completed.event.sequence, 32);
    assert.equal(completed.event.payload.duration_ms, 84);
    assert.equal("result" in completed.event.payload, false);
  });

  it("失败只保存稳定错误码，并拒绝越权工具、敏感摘要和非法终态数据", () => {
    assert.throws(
      () => startAgentToolCall({
        session: runningSession(),
        messageId: "message-user-1",
        callId: "tool-call-2",
        ordinal: 1,
        toolName: "import-file",
        argumentsSummary: {},
        startedAt: "2026-08-31T08:01:01Z",
        lastEventSequence: 30,
      }),
      AgentToolPolicyError,
    );
    assert.throws(
      () => startAgentToolCall({
        session: runningSession(),
        messageId: "message-user-1",
        callId: "tool-call-2",
        ordinal: 1,
        toolName: "summary",
        argumentsSummary: { access_token: "secret" },
        startedAt: "2026-08-31T08:01:01Z",
        lastEventSequence: 30,
      }),
      AgentToolPolicyError,
    );

    const started = startAgentToolCall({
      session: runningSession(),
      messageId: "message-user-1",
      callId: "tool-call-3",
      ordinal: 1,
      toolName: "summary",
      argumentsSummary: {},
      startedAt: "2026-08-31T08:01:01Z",
      lastEventSequence: 30,
    });
    const failed = completeAgentToolCall({
      call: started.call,
      outcome: "failed",
      resultSummary: null,
      durationMs: 12,
      completedAt: "2026-08-31T08:01:02Z",
      lastEventSequence: 31,
    });
    assert.equal(failed.call.errorCode, "AGENT_TOOL_EXECUTION_FAILED");
    assert.equal(failed.call.resultSummary, null);
    assert.equal(failed.event.payload.error_code, "AGENT_TOOL_EXECUTION_FAILED");
    assert.throws(
      () => completeAgentToolCall({
        call: started.call,
        outcome: "failed",
        resultSummary: { partial_rows: 1 },
        durationMs: 12,
        completedAt: "2026-08-31T08:01:02Z",
        lastEventSequence: 31,
      }),
      RangeError,
    );
  });
});
