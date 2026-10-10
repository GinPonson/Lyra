import type { AgentSessionSnapshot } from "./switching.js";
import type { FinancialToolDefinition } from "./tool-registry.js";
import type { AgentTurnStreamSignal } from "./turn-stream.js";

/**
 * 一个 Genkit 回合的边界。模型只能请求服务器提供的只读金融工具；工具通过
 * 绑定当前回合的函数调用进入审计编排器，不暴露 HTTP、MCP 或进程能力。
 * session.messages 包含已持久化历史与本次用户消息，system 口径每轮重新生成。
 */
export type AgentTurnExecutor = (input: {
  session: AgentSessionSnapshot;
  adapterInput: { messageId: string; content: string };
  toolDefinitions: readonly FinancialToolDefinition[];
  executeTool: (request: {
    toolName: string;
    args: unknown;
    abortSignal?: AbortSignal;
  }) => Promise<{ data: unknown }>;
  emitSignal: (signal: AgentTurnStreamSignal) => void;
  abortSignal: AbortSignal;
}) => Promise<void>;
