import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { invokeAuthenticatedAgentTool } from "../src/application/index.js";
import {
  AgentMcpCredentialError,
  FinancialToolRegistry,
} from "../src/domain/agent/index.js";
import type { AgentSessionSnapshot } from "../src/domain/agent/index.js";
import { issueAgentMcpCredential } from "../src/infrastructure/agents/index.js";

/** 构造工具调用期间保持 running 的规范会话。 */
function runningSession(): AgentSessionSnapshot {
  return {
    id: "session-tool-1",
    provider: "claude",
    status: "running",
    accountScope: { kind: "account", account: "TST001" },
    ledgerVersion: 21,
    asOf: "2026-08-31T02:00:00.000Z",
    revision: 7,
    messages: [],
  };
}

describe("invokeAuthenticatedAgentTool", () => {
  it("认证后仅使用服务端范围执行金融工具，并返回凭据使用时间", async () => {
    const session = runningSession();
    const issued = issueAgentMcpCredential({
      session,
      now: new Date("2026-08-31T02:00:00.000Z"),
      ttlMilliseconds: 60_000,
    });
    const registry = new FinancialToolRegistry({
      summary: async (invocation) => invocation,
    });

    const executed = await invokeAuthenticatedAgentTool({
      session,
      credentialRecord: issued.record,
      credentialToken: issued.token,
      registry,
      toolName: "summary",
      arguments: {
        accountScope: { kind: "all" },
        ledger_version: 999,
        asOf: "2099-01-01T00:00:00.000Z",
        currency: "USD",
      },
      now: new Date("2026-08-31T02:00:15.000Z"),
    });

    assert.deepEqual(executed.result, {
      kind: "lyra_financial_data",
      toolName: "summary",
      provenance: {
        accountScope: { kind: "account", account: "TST001" },
        ledgerVersion: 21,
        asOf: "2026-08-31T02:00:00.000Z",
        instructionBoundary: "data_only",
      },
      data: {
        arguments: { currency: "USD" },
        context: {
          accountScope: { kind: "account", account: "TST001" },
          ledgerVersion: 21,
          asOf: "2026-08-31T02:00:00.000Z",
        },
      },
    });
    assert.equal(
      executed.credentialRecord.lastUsedAt,
      "2026-08-31T02:00:15.000Z",
    );
  });

  it("凭据无效时在 handler 执行前终止调用", async () => {
    const session = runningSession();
    const issued = issueAgentMcpCredential({
      session,
      now: new Date("2026-08-31T02:00:00.000Z"),
      ttlMilliseconds: 60_000,
    });
    let invocationCount = 0;
    const registry = new FinancialToolRegistry({
      positions: async () => {
        invocationCount += 1;
        return [];
      },
    });

    await assert.rejects(
      invokeAuthenticatedAgentTool({
        session: { ...session, revision: 8 },
        credentialRecord: issued.record,
        credentialToken: issued.token,
        registry,
        toolName: "positions",
        arguments: {},
        now: new Date("2026-08-31T02:00:10.000Z"),
      }),
      (error: unknown) => (
        error instanceof AgentMcpCredentialError
        && error.internalReason === "session_mismatch"
      ),
    );
    assert.equal(invocationCount, 0);
  });
});
