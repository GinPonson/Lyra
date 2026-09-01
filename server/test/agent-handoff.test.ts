import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AgentSwitchError,
  buildBoundedAgentHandoff,
} from "../src/domain/agent/index.js";
import type { AgentHandoffContext } from "../src/domain/agent/index.js";

/** 构造包含三条完整可见消息的跨供应商上下文。 */
function context(summary: string | null): AgentHandoffContext {
  return {
    fromProvider: "claude",
    toProvider: "pi",
    accountScope: { kind: "account", account: "TST276" },
    ledgerVersion: 12,
    asOf: "2026-08-31T00:00:00Z",
    summary,
    omittedMessageCount: 0,
    messages: [
      { id: "m1", role: "user", content: "旧问题", createdAt: "2026-08-31T00:00:00Z" },
      { id: "m2", role: "assistant", content: "较长的旧回答", createdAt: "2026-08-31T00:01:00Z" },
      { id: "m3", role: "user", content: "最新问题", createdAt: "2026-08-31T00:02:00Z" },
    ],
  };
}

describe("bounded Agent handoff", () => {
  it("使用既有摘要覆盖旧消息，只保留预算内连续的最近完整消息", () => {
    const handoff = buildBoundedAgentHandoff(context("旧消息已归纳"), 11);

    assert.equal(handoff.summary, "旧消息已归纳");
    assert.equal(handoff.omittedMessageCount, 2);
    assert.deepEqual(handoff.messages.map((message) => message.id), ["m3"]);
    assert.equal(handoff.ledgerVersion, 12);
    assert.deepEqual(handoff.accountScope, { kind: "account", account: "TST276" });
  });

  it("没有摘要需要省略消息或最新消息无法完整容纳时明确拒绝切换", () => {
    for (const [handoff, budget] of [
      [context(null), 5],
      [context("摘要"), 4],
    ] as const) {
      assert.throws(
        () => buildBoundedAgentHandoff(handoff, budget),
        (error: unknown) => error instanceof AgentSwitchError
          && error.code === "AGENT_HANDOFF_CONTEXT_TOO_LARGE",
      );
    }
  });
});
