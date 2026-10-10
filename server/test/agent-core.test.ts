import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AgentToolPolicyError,
  FinancialToolRegistry,
} from "../src/domain/agent/index.js";
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
