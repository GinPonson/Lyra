import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AgentSwitchError,
  createAgentSession,
} from "../src/domain/agent/index.js";

describe("Agent session creation", () => {
  it("创建固定供应商、账户范围和账本基线的 idle 空会话", () => {
    const session = createAgentSession({
      sessionId: "session-create-1",
      provider: "pi",
      availability: { provider: "pi", status: "available" },
      accountScope: { kind: "account", account: " TST276 " },
      ledgerVersion: 31,
      asOf: " 2026-08-31T04:00:00.000Z ",
    });

    assert.deepEqual(session, {
      id: "session-create-1",
      provider: "pi",
      status: "idle",
      accountScope: { kind: "account", account: "TST276" },
      ledgerVersion: 31,
      asOf: "2026-08-31T04:00:00.000Z",
      revision: 1,
      summary: null,
      messages: [],
    });
  });

  it("目标 Agent 不可用时明确失败且不自动选择其他供应商", () => {
    assert.throws(
      () => createAgentSession({
        sessionId: "session-create-2",
        provider: "claude",
        availability: {
          provider: "claude",
          status: "unavailable",
          reason: "本机未登录",
        },
        accountScope: { kind: "all" },
        ledgerVersion: 31,
        asOf: "2026-08-31T04:00:00.000Z",
      }),
      (error: unknown) => (
        error instanceof AgentSwitchError
        && error.code === "AGENT_UNAVAILABLE"
        && error.internalReason === "本机未登录"
        && !error.message.includes("未登录")
      ),
    );
  });
});
