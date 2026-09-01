import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  OpenCodeAcpProtocolError,
  OpenCodeAcpTurnAdapter,
  buildOpenCodeCancelNotification,
  buildOpenCodeInitializeRequest,
  buildOpenCodeNewSessionRequest,
  buildOpenCodePromptRequest,
  parseOpenCodeInitializeResponse,
  parseOpenCodeNewSessionResponse,
} from "../src/infrastructure/agents/index.js";

describe("OpenCode ACP adapter", () => {
  it("构造最小能力握手、唯一 Lyra MCP 会话和文本回合请求", () => {
    assert.deepEqual(buildOpenCodeInitializeRequest("init-1", "0.1.0"), {
      jsonrpc: "2.0",
      id: "init-1",
      method: "initialize",
      params: {
        protocolVersion: 1,
        clientCapabilities: {
          auth: { terminal: false },
          fs: { readTextFile: false, writeTextFile: false },
          terminal: false,
        },
        clientInfo: { name: "Lyra", version: "0.1.0" },
      },
    });
    assert.deepEqual(parseOpenCodeInitializeResponse({
      jsonrpc: "2.0",
      id: "init-1",
      result: {
        protocolVersion: 1,
        agentInfo: { name: "OpenCode", version: "1.18.21" },
        agentCapabilities: { mcpCapabilities: { http: true, sse: true } },
      },
    }, "init-1"), {
      protocolVersion: 1,
      agentName: "OpenCode",
      agentVersion: "1.18.21",
      supportsHttpMcp: true,
    });
    assert.deepEqual(buildOpenCodeNewSessionRequest({
      requestId: "new-1",
      workDirectory: "/var/lib/lyra/agents/session-1",
      mcpServer: {
        type: "http",
        name: "lyra",
        url: "http://127.0.0.1:4310/mcp",
        headers: [{ name: "Authorization", value: "Bearer ephemeral" }],
      },
    }).params, {
      cwd: "/var/lib/lyra/agents/session-1",
      mcpServers: [{
        type: "http",
        name: "lyra",
        url: "http://127.0.0.1:4310/mcp",
        headers: [{ name: "Authorization", value: "Bearer ephemeral" }],
      }],
    });
    assert.deepEqual(parseOpenCodeNewSessionResponse({
      jsonrpc: "2.0",
      id: "new-1",
      result: { sessionId: "opencode-session-1" },
    }, "new-1"), { sessionId: "opencode-session-1" });
    assert.deepEqual(buildOpenCodePromptRequest({
      requestId: "prompt-1",
      sessionId: "opencode-session-1",
      content: " 分析组合 ",
    }).params.prompt, [{ type: "text", text: "分析组合" }]);
    assert.deepEqual(buildOpenCodeCancelNotification("opencode-session-1"), {
      jsonrpc: "2.0",
      method: "session/cancel",
      params: { sessionId: "opencode-session-1" },
    });
  });

  it("聚合消息、工具与 usage，并明确拒绝客户端权限和未知协议", () => {
    const adapter = new OpenCodeAcpTurnAdapter({
      sessionId: "session-1",
      promptRequestId: "prompt-1",
    });
    assert.deepEqual(adapter.parseFrame({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId: "session-1",
        update: {
          sessionUpdate: "agent_thought_chunk",
          content: { type: "text", text: "内部分析" },
        },
      },
    }), [{ kind: "reasoning_delta", delta: "内部分析" }]);
    assert.deepEqual(adapter.parseFrame({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId: "session-1",
        update: {
          sessionUpdate: "agent_message_chunk",
          content: { type: "text", text: "最终结论" },
        },
      },
    }), [{ kind: "message_delta", delta: "最终结论" }]);
    assert.deepEqual(adapter.parseFrame({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId: "session-1",
        update: {
          sessionUpdate: "tool_call",
          toolCallId: "tool-1",
          title: "summary",
          kind: "other",
          status: "pending",
          rawInput: { period: "month" },
        },
      },
    }), [{ kind: "tool_started", toolCallId: "tool-1", title: "summary", toolKind: "other" }]);
    assert.deepEqual(adapter.parseFrame({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId: "session-1",
        update: {
          sessionUpdate: "tool_call_update",
          toolCallId: "tool-1",
          status: "completed",
          rawOutput: { rows: 1 },
        },
      },
    }), [{ kind: "tool_completed", toolCallId: "tool-1", isError: false }]);
    assert.deepEqual(adapter.parseFrame({
      jsonrpc: "2.0",
      id: "permission-1",
      method: "session/request_permission",
      params: {
        sessionId: "session-1",
        toolCall: { toolCallId: "shell-1", title: "pwd" },
        options: [{ optionId: "once", kind: "allow_once", name: "Allow once" }],
      },
    }), [{
      kind: "client_response_required",
      response: {
        jsonrpc: "2.0",
        id: "permission-1",
        result: { outcome: { outcome: "cancelled" } },
      },
    }]);
    adapter.parseFrame({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId: "session-1",
        update: {
          sessionUpdate: "usage_update",
          used: 30,
          size: 200_000,
          cost: { amount: 0.02, currency: "USD" },
        },
      },
    });
    assert.deepEqual(adapter.parseFrame({
      jsonrpc: "2.0",
      id: "prompt-1",
      result: {
        stopReason: "end_turn",
        usage: {
          inputTokens: 10,
          outputTokens: 5,
          thoughtTokens: 2,
          cachedReadTokens: 3,
          totalTokens: 20,
        },
      },
    }), [
      { kind: "message_snapshot", content: "最终结论", stopReason: "end_turn" },
      { kind: "usage_updated", totalTokens: 20, totalCost: 0.02 },
      { kind: "turn_settled" },
    ]);
    const unknownAdapter = new OpenCodeAcpTurnAdapter({
      sessionId: "session-1",
      promptRequestId: "prompt-2",
    });
    assert.throws(
      () => unknownAdapter.parseFrame({
        jsonrpc: "2.0",
        method: "session/update",
        params: { sessionId: "session-1", update: { sessionUpdate: "future_update" } },
      }),
      OpenCodeAcpProtocolError,
    );
  });
});
