import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { LATEST_PROTOCOL_VERSION } from "@modelcontextprotocol/sdk/types.js";

import { createServiceHealthSnapshot } from "../src/application/index.js";
import {
  FinancialToolRegistry,
  type AgentSessionSnapshot,
} from "../src/domain/agent/index.js";
import {
  issueAgentMcpCredential,
  type AgentMcpCredentialRecord,
} from "../src/infrastructure/agents/index.js";
import {
  buildHttpServer,
  createServerProtocolMetadata,
} from "../src/transport/index.js";

/** 构造 transport 测试绑定的 running Agent 会话。 */
function runningSession(): AgentSessionSnapshot {
  return {
    id: "session-mcp-http-1",
    provider: "claude",
    status: "running",
    accountScope: { kind: "account", account: "TST001" },
    ledgerVersion: 31,
    asOf: "2026-08-31T10:00:00.000Z",
    revision: 4,
    messages: [],
  };
}

/** 构造带官方 MCP 必需请求头的注入参数。 */
function mcpHeaders(token: string): Record<string, string> {
  return {
    accept: "application/json, text/event-stream",
    authorization: `Bearer ${token}`,
    "content-type": "application/json",
    "mcp-protocol-version": LATEST_PROTOCOL_VERSION,
  };
}

/** 创建只启用 summary 的测试服务与凭据持久化观测器。 */
function createMcpFixture() {
  const session = runningSession();
  const issued = issueAgentMcpCredential({
    session,
    now: new Date("2026-08-31T10:00:00.000Z"),
    ttlMilliseconds: 60_000,
  });
  const persisted: AgentMcpCredentialRecord[] = [];
  let invocationCount = 0;
  const registry = new FinancialToolRegistry({
    summary: async ({ arguments: arguments_, context }) => {
      invocationCount += 1;
      return { arguments: arguments_, context };
    },
  });
  const app = buildHttpServer({
    protocolMetadata: createServerProtocolMetadata("0.1.0", 10),
    getHealthSnapshot: () => createServiceHealthSnapshot({
      database: { status: "available", schemaVersion: 10, ledgerVersion: 31 },
      writeQueue: { accepting: true, pendingOperations: 0 },
      gmailConnections: [],
      agentAvailability: null,
      checkedAt: new Date("2026-08-31T10:00:10.000Z"),
    }),
    mcp: {
      registry,
      loadAuthorization: async (tokenHash) => tokenHash === issued.record.tokenHash
        ? { session, credentialRecord: issued.record }
        : null,
      persistCredentialRecord: async (record) => {
        persisted.push(record);
      },
      now: () => new Date("2026-08-31T10:00:15.000Z"),
    },
  });
  return {
    app,
    issued,
    persisted,
    invocationCount: () => invocationCount,
  };
}

describe("official MCP Streamable HTTP transport", () => {
  it("完成无状态初始化、工具发现和绑定服务端口径的调用", async () => {
    const fixture = createMcpFixture();
    const initialize = await fixture.app.inject({
      method: "POST",
      url: "/mcp",
      headers: mcpHeaders(fixture.issued.token),
      payload: {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: LATEST_PROTOCOL_VERSION,
          capabilities: {},
          clientInfo: { name: "lyra-test", version: "1.0.0" },
        },
      },
    });
    assert.equal(initialize.statusCode, 200);
    assert.equal(initialize.json().result.serverInfo.name, "lyra");
    assert.equal(initialize.headers["mcp-session-id"], undefined);

    const listed = await fixture.app.inject({
      method: "POST",
      url: "/mcp",
      headers: mcpHeaders(fixture.issued.token),
      payload: { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} },
    });
    assert.equal(listed.statusCode, 200);
    assert.deepEqual(
      listed.json().result.tools.map((tool: { name: string }) => tool.name),
      ["summary"],
    );
    assert.equal(listed.json().result.tools[0].annotations.readOnlyHint, true);

    const called = await fixture.app.inject({
      method: "POST",
      url: "/mcp",
      headers: mcpHeaders(fixture.issued.token),
      payload: {
        jsonrpc: "2.0",
        id: 3,
        method: "tools/call",
        params: { name: "summary", arguments: {} },
      },
    });
    assert.equal(called.statusCode, 200);
    assert.equal(called.json().result.structuredContent.kind, "lyra_financial_data");
    assert.deepEqual(called.json().result.structuredContent.provenance, {
      accountScope: { kind: "account", account: "TST001" },
      ledgerVersion: 31,
      asOf: "2026-08-31T10:00:00.000Z",
      instructionBoundary: "data_only",
    });
    assert.equal(fixture.invocationCount(), 1);
    assert.equal(fixture.persisted.length, 3);
    assert.equal(
      fixture.persisted.at(-1)?.lastUsedAt,
      "2026-08-31T10:00:15.000Z",
    );

    await fixture.app.close();
  });

  it("在执行 handler 前拒绝缺失授权、非法参数和未启用工具", async () => {
    const fixture = createMcpFixture();
    const missingAuthorization = await fixture.app.inject({
      method: "POST",
      url: "/mcp",
      headers: {
        accept: "application/json, text/event-stream",
        "content-type": "application/json",
      },
      payload: { jsonrpc: "2.0", id: 1, method: "tools/list", params: {} },
    });
    assert.equal(missingAuthorization.statusCode, 401);
    assert.equal(missingAuthorization.json().error.message, "MCP 会话授权无效");

    const invalidArguments = await fixture.app.inject({
      method: "POST",
      url: "/mcp",
      headers: mcpHeaders(fixture.issued.token),
      payload: {
        jsonrpc: "2.0",
        id: 2,
        method: "tools/call",
        params: { name: "summary", arguments: { accountScope: { kind: "all" } } },
      },
    });
    assert.equal(invalidArguments.statusCode, 200);
    assert.equal(invalidArguments.json().error.code, -32602);

    const unavailableTool = await fixture.app.inject({
      method: "POST",
      url: "/mcp",
      headers: mcpHeaders(fixture.issued.token),
      payload: {
        jsonrpc: "2.0",
        id: 3,
        method: "tools/call",
        params: { name: "positions", arguments: {} },
      },
    });
    assert.equal(unavailableTool.statusCode, 200);
    assert.equal(unavailableTool.json().error.code, -32602);
    assert.equal(fixture.invocationCount(), 0);

    const getRejected = await fixture.app.inject({ method: "GET", url: "/mcp" });
    assert.equal(getRejected.statusCode, 405);
    await fixture.app.close();
  });
});
