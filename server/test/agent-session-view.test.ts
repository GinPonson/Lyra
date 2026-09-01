import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createPublicAgentSession } from "../src/application/index.js";
import type { AgentSessionSnapshot } from "../src/domain/agent/index.js";

describe("public Agent session", () => {
  it("公开规范消息和权威数据基线且隔离内部 handoff 状态", () => {
    const session: AgentSessionSnapshot = {
      id: "session-1",
      provider: "claude",
      status: "idle",
      accountScope: { kind: "account", account: "account-uuid-1" },
      ledgerVersion: 17,
      asOf: "2026-08-31T14:00:00.000Z",
      revision: 4,
      summary: "INTERNAL HANDOFF SUMMARY",
      messages: [
        {
          id: "message-user-1",
          role: "user",
          content: "解释本月表现",
          createdAt: "2026-08-31T13:59:00.000Z",
        },
        {
          id: "message-assistant-1",
          role: "assistant",
          content: "本月已实现收益为……",
          createdAt: "2026-08-31T14:00:00.000Z",
          status: "completed",
          errorCode: null,
        },
      ],
    };

    const result = createPublicAgentSession(session);

    assert.deepEqual(result, {
      id: "session-1",
      provider: "claude",
      status: "idle",
      accountScope: { kind: "account", account: "account-uuid-1" },
      ledgerVersion: 17,
      asOf: "2026-08-31T14:00:00.000Z",
      revision: 4,
      messages: [
        {
          id: "message-user-1",
          role: "user",
          content: "解释本月表现",
          createdAt: "2026-08-31T13:59:00.000Z",
        },
        {
          id: "message-assistant-1",
          role: "assistant",
          content: "本月已实现收益为……",
          createdAt: "2026-08-31T14:00:00.000Z",
          status: "completed",
          errorCode: null,
        },
      ],
    });
    const serialized = JSON.stringify(result);
    assert.equal(serialized.includes("summary"), false);
    assert.equal(serialized.includes("INTERNAL HANDOFF SUMMARY"), false);

    // 返回对象必须与仓储快照解耦，客户端侧修改不能污染服务端内存对象。
    (result.accountScope as { kind: "account"; account: string }).account = "changed";
    (result.messages[0] as { content: string }).content = "changed";
    assert.deepEqual(session.accountScope, {
      kind: "account",
      account: "account-uuid-1",
    });
    assert.equal(session.messages[0]?.content, "解释本月表现");
  });
});
