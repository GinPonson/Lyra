import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  applyAgentTurnStreamSignal,
  createAgentTurnStreamState,
  finalizeAgentTurnStream,
  transitionAgentSession,
} from "../src/domain/agent/index.js";
import type { AgentSessionSnapshot } from "../src/domain/agent/index.js";

/** 构造已经开始逻辑回合的 Agent 会话。 */
function runningSession(): AgentSessionSnapshot {
  return {
    id: "session-stream-1",
    provider: "genkit",
    status: "running",
    accountScope: { kind: "all" },
    ledgerVersion: 61,
    asOf: "2026-08-31T10:00:00Z",
    revision: 17,
    messages: [{
      id: "message-user-stream",
      role: "user",
      content: "解释结果",
      createdAt: "2026-08-31T10:01:00Z",
    }],
  };
}

describe("Agent turn stream aggregation", () => {
  it("实时转发可见文本且 reasoning 只公开状态，并用权威快照完成回合", () => {
    const session = runningSession();
    let state = createAgentTurnStreamState({
      session,
      messageId: "message-assistant-stream",
      lastEventSequence: 50,
    });
    const text = applyAgentTurnStreamSignal({
      state,
      session,
      signal: { kind: "message_delta", delta: "临时" },
      occurredAt: "2026-08-31T10:01:01Z",
    });
    state = text.state;
    const reasoning = applyAgentTurnStreamSignal({
      state,
      session,
      signal: { kind: "reasoning_delta", delta: "隐藏分析" },
      occurredAt: "2026-08-31T10:01:02Z",
    });
    state = reasoning.state;
    state = applyAgentTurnStreamSignal({
      state,
      session,
      signal: { kind: "message_snapshot", content: "权威最终回答", stopReason: "stop" },
      occurredAt: "2026-08-31T10:01:03Z",
    }).state;
    state = applyAgentTurnStreamSignal({
      state,
      session,
      signal: { kind: "turn_settled" },
      occurredAt: "2026-08-31T10:01:04Z",
    }).state;
    const final = finalizeAgentTurnStream({
      state,
      session,
      finishedAt: "2026-08-31T10:01:05Z",
    });

    assert.equal(text.event?.type, "message.delta");
    // App 读取 `payload.text` 累积流式正文；键名漂移会让增量在客户端被静默丢弃。
    assert.equal(text.event?.payload.text, "临时");
    assert.equal("delta" in (text.event?.payload ?? {}), false);
    assert.equal(reasoning.event?.type, "reasoning.delta");
    assert.deepEqual(reasoning.event?.payload, {
      state: "active",
      provider: "genkit",
      ledger_version: 61,
    });
    assert.equal(JSON.stringify(reasoning.event).includes("隐藏分析"), false);
    assert.equal(state.visibleContent, "权威最终回答");
    assert.equal(state.visibleContent.includes("隐藏分析"), false);
    assert.equal(final.assistantMessage?.content, "权威最终回答");
    assert.equal(final.event.type, "message.completed");
    assert.equal(final.event.sequence, 53);
  });

  it("只有 settled 才能收口；取消保留可见部分并拒绝终态后追加", () => {
    const running = runningSession();
    let state = createAgentTurnStreamState({
      session: running,
      messageId: "message-assistant-cancelled",
      lastEventSequence: 60,
    });
    state = applyAgentTurnStreamSignal({
      state,
      session: running,
      signal: { kind: "message_delta", delta: "已读取持仓。" },
      occurredAt: "2026-08-31T10:02:01Z",
    }).state;
    assert.throws(
      () => finalizeAgentTurnStream({
        state,
        session: running,
        finishedAt: "2026-08-31T10:02:02Z",
      }),
      RangeError,
    );
    state = applyAgentTurnStreamSignal({
      state,
      session: running,
      signal: { kind: "message_failed", outcome: "cancelled" },
      occurredAt: "2026-08-31T10:02:02Z",
    }).state;
    state = applyAgentTurnStreamSignal({
      state,
      session: running,
      signal: { kind: "turn_settled" },
      occurredAt: "2026-08-31T10:02:03Z",
    }).state;
    const final = finalizeAgentTurnStream({
      state,
      session: transitionAgentSession(running, "cancelling"),
      finishedAt: "2026-08-31T10:02:04Z",
    });

    assert.equal(final.assistantMessage?.status, "cancelled");
    assert.equal(final.assistantMessage?.content, "已读取持仓。");
    assert.equal(final.event.payload.error_code, "AGENT_TURN_CANCELLED");
    assert.throws(
      () => applyAgentTurnStreamSignal({
        state,
        session: running,
        signal: { kind: "message_delta", delta: "迟到数据" },
        occurredAt: "2026-08-31T10:02:05Z",
      }),
      RangeError,
    );
  });
});
