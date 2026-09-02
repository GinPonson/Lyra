import assert from "node:assert/strict";
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import type {
  AgentProvider,
  AgentSessionSnapshot,
  AgentTurnStreamSignal,
} from "../src/domain/agent/index.js";
import { createAgentTurnExecutors } from "../src/infrastructure/agents/index.js";

const MCP_ENDPOINT = "http://127.0.0.1:3141/mcp";
// 与签发器一致的 256-bit Base64URL 形状（≥40 字符），满足 MCP HTTP 配置校验。
const MCP_BEARER = "A7fKx2mQ9pLw4nRt8vBc1dEe5fGg6hHh7iIi8jJj9kKk0lLl1mMm2nNn";

const FAKE_CLAUDE_OK = `#!${process.execPath}
import { createInterface } from "node:readline";
const out = (frame) => process.stdout.write(\`\${JSON.stringify(frame)}\\n\`);
createInterface({ input: process.stdin }).on("line", (line) => {
  const frame = JSON.parse(line);
  if (frame.type === "user") {
    out({ type: "system", subtype: "init", session_id: "fake" });
    out({ type: "stream_event", event: { type: "content_block_delta", delta: { type: "thinking_delta", thinking: "思考" } } });
    out({ type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "Claude 部分" } } });
    out({ type: "assistant", message: { role: "assistant", content: [{ type: "tool_use", id: "toolu-1", name: "mcp__lyra__positions", input: {} }] } });
    out({ type: "control_request", request_id: "perm-1", request: { subtype: "can_use_tool", tool_name: "Bash", tool_use_id: "toolu-2", input: { command: "x" } } });
  } else if (frame.type === "control_response") {
    if (frame.response.response.behavior !== "deny") process.exit(3);
    out({ type: "result", subtype: "success", is_error: false, result: "Claude 完成", stop_reason: null, usage: { input_tokens: 1, output_tokens: 2, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 }, total_cost_usd: 0.02 });
    process.exit(0);
  }
});
`;

const FAKE_CLAUDE_HANG = `#!${process.execPath}
process.stdin.resume();
setInterval(() => {}, 10_000);
`;

const FAKE_CLAUDE_INVALID = `#!${process.execPath}
process.stdin.resume();
process.stdout.write(\`\${JSON.stringify({ type: "bogus-frame" })}\\n\`);
setInterval(() => {}, 10_000);
`;

const FAKE_PI_OK = `#!${process.execPath}
import { createInterface } from "node:readline";
const out = (frame) => process.stdout.write(\`\${JSON.stringify(frame)}\\n\`);
createInterface({ input: process.stdin }).on("line", (line) => {
  const frame = JSON.parse(line);
  if (frame.type === "prompt") {
    out({ type: "response", command: "prompt", success: true, id: frame.id });
    out({ type: "message_update", assistantMessageEvent: { type: "text_delta", delta: "Pi 部分" } });
    out({ type: "message_update", assistantMessageEvent: { type: "text_delta", delta: "文本" } });
    out({ type: "message_end", message: { role: "assistant", stopReason: "stop", content: [{ type: "text", text: "Pi 最终文本" }], usage: { totalTokens: 9, cost: { total: 0.02 } } } });
    out({ type: "agent_settled" });
  }
});
setInterval(() => {}, 10_000);
`;

const FAKE_OPENCODE_OK = `#!${process.execPath}
import { createInterface } from "node:readline";
const out = (frame) => process.stdout.write(\`\${JSON.stringify(frame)}\\n\`);
createInterface({ input: process.stdin }).on("line", (line) => {
  const frame = JSON.parse(line);
  if (frame.method === "initialize") {
    out({ jsonrpc: "2.0", id: frame.id, result: { protocolVersion: 1, agentInfo: { name: "opencode", version: "1.18.21" }, agentCapabilities: { mcpCapabilities: { http: true } } } });
  } else if (frame.method === "session/new") {
    const server = frame.params.mcpServers[0];
    const bearer = Array.isArray(server.headers)
      ? server.headers.find((header) => header.name === "authorization")
      : null;
    if (server.type !== "http" || server.name !== "lyra" || bearer === null || !bearer.value.startsWith("Bearer ")) {
      process.exit(3);
    }
    out({ jsonrpc: "2.0", id: frame.id, result: { sessionId: "oc-1" } });
  } else if (frame.method === "session/prompt") {
    out({ jsonrpc: "2.0", method: "session/update", params: { sessionId: "oc-1", update: { sessionUpdate: "agent_thought_chunk", content: { type: "text", text: "想" } } } });
    out({ jsonrpc: "2.0", method: "session/update", params: { sessionId: "oc-1", update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "OpenCode 部分" } } } });
    out({ jsonrpc: "2.0", method: "session/update", params: { sessionId: "oc-1", update: { sessionUpdate: "tool_call", toolCallId: "tc-1", title: "读取持仓", kind: "read" } } });
    out({ jsonrpc: "2.0", id: frame.id, result: { stopReason: "end_turn", usage: { inputTokens: 3, outputTokens: 4, totalTokens: 7 } } });
  }
});
setInterval(() => {}, 10_000);
`;

/** 写入全部受控假 CLI；以当前 node 作为解释器，避免依赖子进程 PATH。 */
async function writeFakeClis(): Promise<{ fixturesDirectory: string; workspaceRoot: string }> {
  const fixturesDirectory = await mkdtemp(join(tmpdir(), "lyra-fake-clis-"));
  const workspaceRoot = await mkdtemp(join(tmpdir(), "lyra-executor-workspaces-"));
  const scripts: Readonly<Record<string, string>> = {
    "fake-claude-ok.mjs": FAKE_CLAUDE_OK,
    "fake-claude-hang.mjs": FAKE_CLAUDE_HANG,
    "fake-claude-invalid.mjs": FAKE_CLAUDE_INVALID,
    "fake-pi-ok.mjs": FAKE_PI_OK,
    "fake-pi-extension.mjs": "export default {};\n",
    "fake-opencode-ok.mjs": FAKE_OPENCODE_OK,
  };
  for (const [name, content] of Object.entries(scripts)) {
    const filePath = join(fixturesDirectory, name);
    await writeFile(filePath, content);
    await chmod(filePath, 0o755);
  }
  return { fixturesDirectory, workspaceRoot };
}

function fakeSession(provider: AgentProvider): AgentSessionSnapshot {
  return {
    id: "88000000-0000-4000-8000-000000000001",
    provider,
    status: "running",
    accountScope: { kind: "all" },
    ledgerVersion: 1,
    asOf: "2026-09-01T09:00:00.000Z",
    revision: 2,
    messages: [{
      id: "89000000-0000-4000-8000-000000000001",
      role: "user",
      content: "分析当前持仓",
      createdAt: "2026-09-01T09:01:00.000Z",
    }],
  };
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, milliseconds);
  });
}

/** 限定执行器调用时长，防止缺陷导致测试套件悬挂。 */
async function withTimeout<T>(promise: Promise<T>, label: string): Promise<T> {
  let timer: NodeJS.Timeout | null = null;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} 超时`)), 8_000);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    if (timer !== null) {
      clearTimeout(timer);
    }
  }
}

describe("provider turn executors", () => {
  it("Claude 执行器翻译 stream-json 并拒绝越权工具权限", async () => {
    const { fixturesDirectory, workspaceRoot } = await writeFakeClis();
    try {
      const executors = createAgentTurnExecutors({
        workspaceRoot,
        clientVersion: "0.1.0",
        executables: {
          claude: join(fixturesDirectory, "fake-claude-ok.mjs"),
          pi: join(fixturesDirectory, "fake-pi-ok.mjs"),
          opencode: join(fixturesDirectory, "fake-opencode-ok.mjs"),
        },
        piExtensionPath: join(fixturesDirectory, "fake-pi-extension.mjs"),
        turnTimeoutMilliseconds: 10_000,
      });
      const signals: AgentTurnStreamSignal[] = [];
      await withTimeout(executors.claude({
        session: fakeSession("claude"),
        adapterInput: { messageId: "m-1", content: "分析当前持仓" },
        handoffContext: null,
        mcp: { endpoint: MCP_ENDPOINT, bearer: MCP_BEARER },
        emitSignal: (signal) => {
          signals.push(signal);
        },
        abortSignal: new AbortController().signal,
      }), "Claude 执行器");
      assert.deepEqual(signals, [
        { kind: "reasoning_delta", delta: "思考" },
        { kind: "message_delta", delta: "Claude 部分" },
        { kind: "message_snapshot", content: "Claude 完成", stopReason: "end_turn" },
        { kind: "usage_updated", totalTokens: 3, totalCost: 0.02 },
        { kind: "turn_settled" },
      ]);
    } finally {
      await rm(fixturesDirectory, { recursive: true, force: true });
      await rm(workspaceRoot, { recursive: true, force: true });
    }
  });

  it("取消中的 Claude 回合按 cancelled 收口且进程被回收", async () => {
    const { fixturesDirectory, workspaceRoot } = await writeFakeClis();
    try {
      const executors = createAgentTurnExecutors({
        workspaceRoot,
        clientVersion: "0.1.0",
        executables: {
          claude: join(fixturesDirectory, "fake-claude-hang.mjs"),
          pi: join(fixturesDirectory, "fake-pi-ok.mjs"),
          opencode: join(fixturesDirectory, "fake-opencode-ok.mjs"),
        },
        piExtensionPath: join(fixturesDirectory, "fake-pi-extension.mjs"),
        turnTimeoutMilliseconds: 10_000,
      });
      const signals: AgentTurnStreamSignal[] = [];
      const controller = new AbortController();
      const run = executors.claude({
        session: fakeSession("claude"),
        adapterInput: { messageId: "m-1", content: "长时间分析" },
        handoffContext: null,
        mcp: { endpoint: MCP_ENDPOINT, bearer: MCP_BEARER },
        emitSignal: (signal) => {
          signals.push(signal);
        },
        abortSignal: controller.signal,
      });
      await sleep(300);
      controller.abort();
      await withTimeout(run, "Claude 取消");
      assert.deepEqual(signals, [
        { kind: "message_failed", outcome: "cancelled" },
        { kind: "turn_settled" },
      ]);
    } finally {
      await rm(fixturesDirectory, { recursive: true, force: true });
      await rm(workspaceRoot, { recursive: true, force: true });
    }
  });

  it("协议异常按 failed 收口", async () => {
    const { fixturesDirectory, workspaceRoot } = await writeFakeClis();
    try {
      const executors = createAgentTurnExecutors({
        workspaceRoot,
        clientVersion: "0.1.0",
        executables: {
          claude: join(fixturesDirectory, "fake-claude-invalid.mjs"),
          pi: join(fixturesDirectory, "fake-pi-ok.mjs"),
          opencode: join(fixturesDirectory, "fake-opencode-ok.mjs"),
        },
        piExtensionPath: join(fixturesDirectory, "fake-pi-extension.mjs"),
        turnTimeoutMilliseconds: 10_000,
      });
      const signals: AgentTurnStreamSignal[] = [];
      await withTimeout(executors.claude({
        session: fakeSession("claude"),
        adapterInput: { messageId: "m-1", content: "分析" },
        handoffContext: null,
        mcp: { endpoint: MCP_ENDPOINT, bearer: MCP_BEARER },
        emitSignal: (signal) => {
          signals.push(signal);
        },
        abortSignal: new AbortController().signal,
      }), "Claude 协议异常");
      assert.deepEqual(signals, [
        { kind: "message_failed", outcome: "failed" },
        { kind: "turn_settled" },
      ]);
    } finally {
      await rm(fixturesDirectory, { recursive: true, force: true });
      await rm(workspaceRoot, { recursive: true, force: true });
    }
  });

  it("Pi 执行器翻译 RPC 并在结算后回收持久进程", async () => {
    const { fixturesDirectory, workspaceRoot } = await writeFakeClis();
    try {
      const executors = createAgentTurnExecutors({
        workspaceRoot,
        clientVersion: "0.1.0",
        executables: {
          claude: join(fixturesDirectory, "fake-claude-ok.mjs"),
          pi: join(fixturesDirectory, "fake-pi-ok.mjs"),
          opencode: join(fixturesDirectory, "fake-opencode-ok.mjs"),
        },
        piExtensionPath: join(fixturesDirectory, "fake-pi-extension.mjs"),
        turnTimeoutMilliseconds: 10_000,
      });
      const signals: AgentTurnStreamSignal[] = [];
      await withTimeout(executors.pi({
        session: fakeSession("pi"),
        adapterInput: { messageId: "m-1", content: "解释风险" },
        handoffContext: null,
        mcp: { endpoint: MCP_ENDPOINT, bearer: MCP_BEARER },
        emitSignal: (signal) => {
          signals.push(signal);
        },
        abortSignal: new AbortController().signal,
      }), "Pi 执行器");
      assert.deepEqual(signals, [
        { kind: "message_delta", delta: "Pi 部分" },
        { kind: "message_delta", delta: "文本" },
        { kind: "message_snapshot", content: "Pi 最终文本", stopReason: "stop" },
        { kind: "usage_updated", totalTokens: 9, totalCost: 0.02 },
        { kind: "turn_settled" },
      ]);
    } finally {
      await rm(fixturesDirectory, { recursive: true, force: true });
      await rm(workspaceRoot, { recursive: true, force: true });
    }
  });

  it("OpenCode 执行器完成 ACP 握手并携带 Lyra MCP 短期凭据", async () => {
    const { fixturesDirectory, workspaceRoot } = await writeFakeClis();
    try {
      const executors = createAgentTurnExecutors({
        workspaceRoot,
        clientVersion: "0.1.0",
        executables: {
          claude: join(fixturesDirectory, "fake-claude-ok.mjs"),
          pi: join(fixturesDirectory, "fake-pi-ok.mjs"),
          opencode: join(fixturesDirectory, "fake-opencode-ok.mjs"),
        },
        piExtensionPath: join(fixturesDirectory, "fake-pi-extension.mjs"),
        turnTimeoutMilliseconds: 10_000,
      });
      const signals: AgentTurnStreamSignal[] = [];
      await withTimeout(executors.opencode({
        session: fakeSession("opencode"),
        adapterInput: { messageId: "m-1", content: "总结持仓" },
        handoffContext: null,
        mcp: { endpoint: MCP_ENDPOINT, bearer: MCP_BEARER },
        emitSignal: (signal) => {
          signals.push(signal);
        },
        abortSignal: new AbortController().signal,
      }), "OpenCode 执行器");
      assert.deepEqual(signals, [
        { kind: "reasoning_delta", delta: "想" },
        { kind: "message_delta", delta: "OpenCode 部分" },
        { kind: "message_snapshot", content: "OpenCode 部分", stopReason: "end_turn" },
        { kind: "usage_updated", totalTokens: 7, totalCost: 0 },
        { kind: "turn_settled" },
      ]);
    } finally {
      await rm(fixturesDirectory, { recursive: true, force: true });
      await rm(workspaceRoot, { recursive: true, force: true });
    }
  });
});
