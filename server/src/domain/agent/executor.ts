import type { AgentSessionSnapshot } from "./switching.js";
import type { AgentTurnStreamSignal } from "./turn-stream.js";

/**
 * 单个供应商回合的执行边界。
 *
 * 执行器把 Claude/Pi/OpenCode 的协议帧翻译为统一流信号，并保证恰好以
 * `turn_settled` 结束；取消通过 `abortSignal` 请求，执行器应中断子进程并把
 * 当前回合表达为 `message_failed(cancelled)`。`handoffContext` 携带切换后
 * 的一次性上下文，执行器必须把它并入本回合 prompt。金融工具不经过执行器：
 * CLI 经 MCP HTTP 直接调用 Lyra，工具调用持久化由 orchestrator 统一处理。
 */
export type AgentTurnExecutor = (input: {
  session: AgentSessionSnapshot;
  adapterInput: { messageId: string; content: string };
  /** 跨供应商切换后首个回合的一次性 handoff 上下文；无切换为 null。 */
  handoffContext: string | null;
  mcp: { endpoint: string; bearer: string };
  emitSignal: (signal: AgentTurnStreamSignal) => void;
  abortSignal: AbortSignal;
}) => Promise<void>;
