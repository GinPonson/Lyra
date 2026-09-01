import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AgentSwitchError,
  AgentToolPolicyError,
  FinancialToolRegistry,
  prepareAgentSwitch,
} from "../src/domain/agent/index.js";
import type { AgentSessionSnapshot } from "../src/domain/agent/index.js";

/** 构造位于完整回合边界的 Claude 会话。 */
function session(status: AgentSessionSnapshot["status"] = "idle"): AgentSessionSnapshot {
  return {
    id: "session-1",
    provider: "claude",
    status,
    accountScope: { kind: "account", account: "TST001" },
    ledgerVersion: 12,
    asOf: "2026-08-30T00:00:00Z",
    revision: 3,
    messages: [{
      id: "message-1",
      role: "user",
      content: "解释当前持仓",
      createdAt: "2026-08-30T00:00:00Z",
    }],
  };
}

describe("FinancialToolRegistry", () => {
  it("仅调用已配置只读工具，并用服务端上下文覆盖范围字段", async () => {
    const registry = new FinancialToolRegistry({
      summary: async (invocation) => invocation,
    });
    const context = {
      accountScope: { kind: "account", account: "TST001" } as const,
      ledgerVersion: 12,
      asOf: "2026-08-30T00:00:00Z",
    };
    const result = await registry.invoke("summary", {
      account_scope: "all",
      ledgerVersion: 999,
      groupBy: "asset-type",
    }, context);

    assert.deepEqual(registry.availableTools(), ["summary"]);
    assert.deepEqual(result, {
      arguments: { groupBy: "asset-type" },
      context,
    });
  });

  it("拒绝导入类工具和白名单内但未配置的工具", async () => {
    const registry = new FinancialToolRegistry({});
    await assert.rejects(
      registry.invoke("import-file", {}, {
        accountScope: { kind: "all" }, ledgerVersion: 1, asOf: "2026-08-30",
      }),
      (error: unknown) => error instanceof AgentToolPolicyError && error.message.includes("白名单"),
    );
    await assert.rejects(
      registry.invoke("summary", {}, {
        accountScope: { kind: "all" }, ledgerVersion: 1, asOf: "2026-08-30",
      }),
      (error: unknown) => error instanceof AgentToolPolicyError && error.message.includes("尚未配置"),
    );
  });
});

describe("prepareAgentSwitch", () => {
  it("在回合边界保留账户与账本基线并添加明确切换标记", () => {
    const current = session();
    const plan = prepareAgentSwitch(
      current,
      "opencode",
      { provider: "opencode", status: "available" },
      "message-switch-1",
      "2026-08-30T00:01:00Z",
    );

    assert.equal(plan.nextSession.provider, "opencode");
    assert.equal(plan.nextSession.revision, 4);
    assert.deepEqual(plan.nextSession.accountScope, current.accountScope);
    assert.equal(plan.nextSession.ledgerVersion, 12);
    assert.equal(plan.nextSession.messages.at(-1)?.role, "system");
    assert.equal(plan.handoff.messages.at(-1), plan.nextSession.messages.at(-1));
    assert.match(plan.handoff.messages.at(-1)?.content ?? "", /claude.*opencode/);
    assert.deepEqual(plan.handoff.messages.slice(0, -1), current.messages);
    assert.equal(current.provider, "claude");
    assert.equal(current.messages.length, 1);
  });

  it("拒绝回合中切换和不可用目标，不自动选择其他 Agent", () => {
    assert.throws(
      () => prepareAgentSwitch(
        session("running"), "pi", { provider: "pi", status: "available" },
        "switch-1", "2026-08-30T00:01:00Z",
      ),
      (error: unknown) => (
        error instanceof AgentSwitchError
        && error.code === "AGENT_SWITCH_REQUIRES_TURN_BOUNDARY"
      ),
    );
    assert.throws(
      () => prepareAgentSwitch(
        session(), "pi", { provider: "pi", status: "unavailable", reason: "未登录" },
        "switch-2", "2026-08-30T00:01:00Z",
      ),
      (error: unknown) => (
        error instanceof AgentSwitchError
        && error.code === "AGENT_UNAVAILABLE"
        && error.internalReason === "未登录"
        && !error.message.includes("未登录")
      ),
    );
  });
});
