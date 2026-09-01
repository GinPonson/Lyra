import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  PiRpcProtocolError,
  buildPiAbortCommand,
  buildPiPromptCommand,
  parsePiRpcFrame,
} from "../src/infrastructure/agents/index.js";

describe("Pi RPC adapter", () => {
  it("构造关联命令并分离文本、reasoning、工具请求与最终可见快照", () => {
    assert.deepEqual(buildPiPromptCommand("request-1", " 分析组合 "), {
      id: "request-1",
      type: "prompt",
      message: "分析组合",
    });
    assert.deepEqual(parsePiRpcFrame({
      id: "request-1",
      type: "response",
      command: "prompt",
      success: true,
    }), [{ kind: "command_accepted", command: "prompt", requestId: "request-1" }]);
    assert.deepEqual(parsePiRpcFrame({
      type: "message_update",
      usage: { totalTokens: 2, cost: { total: 0.001 } },
      assistantMessageEvent: { type: "text_delta", delta: "结论" },
    }), [{ kind: "message_delta", delta: "结论" }]);
    assert.deepEqual(parsePiRpcFrame({
      type: "message_update",
      usage: { totalTokens: 3, cost: { total: 0.002 } },
      assistantMessageEvent: { type: "thinking_delta", delta: "内部分析" },
    }), [{ kind: "reasoning_delta", delta: "内部分析" }]);
    assert.deepEqual(parsePiRpcFrame({
      type: "message_update",
      usage: { totalTokens: 4, cost: { total: 0.003 } },
      assistantMessageEvent: {
        type: "toolcall_end",
        toolCall: { id: "call-1", name: "summary", arguments: { period: "month" } },
      },
    }), [{
      kind: "tool_requested",
      toolCallId: "call-1",
      toolName: "summary",
      arguments: { period: "month" },
    }]);
    assert.deepEqual(parsePiRpcFrame({
      type: "message_end",
      message: {
        role: "assistant",
        stopReason: "stop",
        content: [
          { type: "thinking", thinking: "不得进入快照" },
          { type: "text", text: "最终结论" },
        ],
        usage: { totalTokens: 10, cost: { total: 0.01 } },
      },
    }), [
      { kind: "message_snapshot", content: "最终结论", stopReason: "stop" },
      { kind: "usage_updated", totalTokens: 10, totalCost: 0.01 },
    ]);
    assert.deepEqual(parsePiRpcFrame({ type: "agent_settled" }), [{ kind: "turn_settled" }]);
  });

  it("构造取消命令，并把供应商错误、扩展 UI 与未知帧明确判为协议错误", () => {
    assert.deepEqual(buildPiAbortCommand("abort-1"), {
      id: "abort-1",
      type: "abort",
    });
    assert.deepEqual(parsePiRpcFrame({
      type: "message_update",
      assistantMessageEvent: { type: "error", reason: "aborted" },
    }), [{ kind: "message_failed", outcome: "cancelled" }]);
    assert.throws(
      () => parsePiRpcFrame({
        type: "response",
        command: "prompt",
        success: false,
        error: "provider token leaked in raw error",
      }),
      (error: unknown) => (
        error instanceof PiRpcProtocolError
        && error.code === "AGENT_PROTOCOL_ERROR"
        && !error.message.includes("token leaked")
        && error.internalReason.includes("token leaked")
      ),
    );
    assert.throws(
      () => parsePiRpcFrame({ type: "extension_ui_request", method: "confirm" }),
      PiRpcProtocolError,
    );
    assert.throws(
      () => parsePiRpcFrame({ type: "future_unknown_event" }),
      PiRpcProtocolError,
    );
  });
});
