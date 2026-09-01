import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ClaudeStreamProtocolError,
  buildClaudeInterruptRequest,
  buildClaudeToolPermissionResponse,
  buildClaudeUserMessage,
  parseClaudeStreamFrame,
} from "../src/infrastructure/agents/index.js";

describe("Claude stream-json adapter", () => {
  it("构造输入帧，并以 partial delta 与 result 权威快照收口回合", () => {
    assert.deepEqual(buildClaudeUserMessage(" 分析组合 "), {
      type: "user",
      session_id: "",
      message: { role: "user", content: "分析组合" },
      parent_tool_use_id: null,
    });
    assert.deepEqual(buildClaudeInterruptRequest("interrupt-1"), {
      type: "control_request",
      request_id: "interrupt-1",
      request: { subtype: "interrupt" },
    });
    assert.deepEqual(parseClaudeStreamFrame({
      type: "stream_event",
      parent_tool_use_id: null,
      event: {
        type: "content_block_delta",
        delta: { type: "text_delta", text: "结论" },
      },
    }), [{ kind: "message_delta", delta: "结论" }]);
    assert.deepEqual(parseClaudeStreamFrame({
      type: "stream_event",
      parent_tool_use_id: null,
      event: {
        type: "content_block_delta",
        delta: { type: "thinking_delta", thinking: "内部分析" },
      },
    }), [{ kind: "reasoning_delta", delta: "内部分析" }]);
    assert.deepEqual(parseClaudeStreamFrame({
      type: "result",
      subtype: "success",
      is_error: false,
      result: "最终结论",
      stop_reason: "end_turn",
      total_cost_usd: 0.01,
      usage: {
        input_tokens: 10,
        output_tokens: 5,
        cache_creation_input_tokens: 2,
        cache_read_input_tokens: 3,
      },
    }), [
      { kind: "message_snapshot", content: "最终结论", stopReason: "end_turn" },
      { kind: "usage_updated", totalTokens: 20, totalCost: 0.01 },
      { kind: "turn_settled" },
    ]);
  });

  it("收窄 Lyra 工具、编码权限响应，并拒绝未知或越界控制协议", () => {
    assert.deepEqual(parseClaudeStreamFrame({
      type: "assistant",
      message: {
        role: "assistant",
        content: [
          {
            type: "tool_use",
            id: "tool-1",
            name: "mcp__lyra__summary",
            input: { period: "month" },
          },
          {
            type: "tool_use",
            id: "tool-2",
            name: "Bash",
            input: { command: "pwd" },
          },
        ],
      },
    }), [
      {
        kind: "tool_requested",
        toolCallId: "tool-1",
        providerToolName: "mcp__lyra__summary",
        toolName: "summary",
        arguments: { period: "month" },
      },
      {
        kind: "tool_requested",
        toolCallId: "tool-2",
        providerToolName: "Bash",
        toolName: null,
        arguments: { command: "pwd" },
      },
    ]);
    assert.deepEqual(parseClaudeStreamFrame({
      type: "control_request",
      request_id: "permission-1",
      request: {
        subtype: "can_use_tool",
        tool_name: "mcp__lyra__positions",
        tool_use_id: "tool-3",
        input: { symbol: "AAPL" },
      },
    }), [{
      kind: "tool_permission_requested",
      controlRequestId: "permission-1",
      toolCallId: "tool-3",
      providerToolName: "mcp__lyra__positions",
      toolName: "positions",
      arguments: { symbol: "AAPL" },
    }]);
    assert.deepEqual(buildClaudeToolPermissionResponse({
      controlRequestId: "permission-1",
      toolCallId: "tool-3",
      decision: { behavior: "allow", updatedInput: { symbol: "AAPL" } },
    }), {
      type: "control_response",
      response: {
        subtype: "success",
        request_id: "permission-1",
        response: {
          behavior: "allow",
          updatedInput: { symbol: "AAPL" },
          toolUseID: "tool-3",
        },
      },
    });
    assert.equal(
      buildClaudeToolPermissionResponse({
        controlRequestId: "permission-2",
        toolCallId: "tool-4",
        decision: { behavior: "deny" },
      }).response.response.behavior,
      "deny",
    );
    assert.throws(
      () => parseClaudeStreamFrame({
        type: "control_request",
        request_id: "request-2",
        request: { subtype: "elicitation" },
      }),
      ClaudeStreamProtocolError,
    );
    assert.throws(
      () => parseClaudeStreamFrame({ type: "future_unknown_event" }),
      ClaudeStreamProtocolError,
    );
  });
});
