import { isAbsolute } from "node:path";
import { randomUUID } from "node:crypto";

import type {
  AgentProvider,
  AgentTurnExecutor,
  AgentTurnStreamSignal,
} from "../../domain/agent/index.js";
import {
  stageClaudeMcpConfig,
  buildAgentCliLaunchPlan,
  startAgentCliProcess,
  createAgentSessionWorkspace,
  buildClaudeToolPermissionResponse,
  buildClaudeUserMessage,
  parseClaudeStreamFrame,
  buildPiPromptCommand,
  parsePiRpcFrame,
  OpenCodeAcpTurnAdapter,
  buildOpenCodeInitializeRequest,
  buildOpenCodeNewSessionRequest,
  buildOpenCodePromptRequest,
  parseOpenCodeInitializeResponse,
  parseOpenCodeNewSessionResponse,
} from "./index.js";
import type {
  AgentCliLaunchPlan,
  AgentCliProcessHandle,
  ClaudeStreamAdapterSignal,
  OpenCodeAcpAdapterSignal,
  PiRpcAdapterSignal,
  StagedClaudeMcpConfig,
} from "./index.js";

/** 三个供应商回合执行器共享的静态运行配置。 */
export interface AgentTurnExecutorFactoryOptions {
  /** 三供应商会话工作目录唯一的受信绝对根路径。 */
  workspaceRoot: string;
  /** OpenCode ACP initialize 暴露的 Lyra 服务版本。 */
  clientVersion: string;
  /** 三个供应商 CLI 的绝对路径；缺失由进程运行时的稳定错误收口。 */
  executables: Readonly<Record<AgentProvider, string>>;
  /** Pi Lyra 扩展文件路径；扩展桥接受阻期间无法真实运行 Pi 回合。 */
  piExtensionPath: string;
  /** 单个回合的硬超时；超时由进程运行时按失败收口。 */
  turnTimeoutMilliseconds: number;
}

/** 单回合的信号出口：结算后丢弃迟到帧，并记录结算状态供骨架等待。 */
interface TurnSignalSink {
  readonly runtime: { settled: boolean };
  readonly settledPromise: Promise<void>;
  emit(signal: AgentTurnStreamSignal): void;
}

function createSignalSink(
  emitSignal: (signal: AgentTurnStreamSignal) => void,
): TurnSignalSink {
  const runtime = { settled: false };
  let settleResolve: () => void = () => undefined;
  const settledPromise = new Promise<void>((resolve) => {
    settleResolve = resolve;
  });
  return {
    runtime,
    settledPromise,
    emit(signal: AgentTurnStreamSignal): void {
      if (runtime.settled) {
        return;
      }
      emitSignal(signal);
      if (signal.kind === "turn_settled") {
        runtime.settled = true;
        settleResolve();
      }
    },
  };
}

/**
 * 供应商共享的回合骨架：启动受限子进程、等待协议结算或进程退出、把异常退
 * 出表达为统一失败/取消信号，并在终态后回收进程与一次性工作目录。
 */
async function runTurnToSettlement(input: {
  plan: AgentCliLaunchPlan;
  timeoutMilliseconds: number;
  abortSignal: AbortSignal;
  sink: TurnSignalSink;
  onFrame: (frame: unknown, handle: AgentCliProcessHandle) => void;
  start: (handle: AgentCliProcessHandle) => Promise<void>;
  dispose: () => Promise<void>;
}): Promise<void> {
  const handle = startAgentCliProcess({
    plan: input.plan,
    timeoutMilliseconds: input.timeoutMilliseconds,
    onFrame: (frame) => input.onFrame(frame, handle),
  });
  const onAbort = (): void => {
    handle.cancel();
  };
  input.abortSignal.addEventListener("abort", onAbort, { once: true });
  let exitObserved = false;
  try {
    const exitWaiter = handle.completion.then(() => {
      exitObserved = true;
    });
    await input.start(handle);
    await Promise.race([input.sink.settledPromise, exitWaiter]);
    if (!input.sink.runtime.settled) {
      // 进程在协议结算前退出：取消中的回合按取消收口，其余按失败收口。
      input.sink.emit({
        kind: "message_failed",
        outcome: input.abortSignal.aborted ? "cancelled" : "failed",
      });
      input.sink.emit({ kind: "turn_settled" });
    }
    if (!exitObserved) {
      // Pi/OpenCode 是持久协议进程；回合结算后即可回收。
      handle.cancel();
    }
    await exitWaiter;
  } finally {
    input.abortSignal.removeEventListener("abort", onAbort);
    handle.cancel();
    await input.dispose();
  }
}

/**
 * 生成三个供应商的回合执行器。
 *
 * 执行器只做进程编排与协议翻译：启动计划来自既有最小权限构造器，帧解析来
 * 自三个协议适配器，金融工具由 CLI 经 MCP HTTP 独立调用并在 orchestrator
 * 持久化，不经过本模块。
 */
export function createAgentTurnExecutors(
  options: AgentTurnExecutorFactoryOptions,
): Readonly<Record<AgentProvider, AgentTurnExecutor>> {
  if (!isAbsolute(options.workspaceRoot)) {
    throw new RangeError("Agent 工作根目录必须是绝对路径");
  }
  if (options.clientVersion.trim().length === 0) {
    throw new RangeError("Lyra 客户端版本不能为空");
  }
  if (!Number.isSafeInteger(options.turnTimeoutMilliseconds) || options.turnTimeoutMilliseconds <= 0) {
    throw new RangeError("Agent 回合超时必须是正安全整数");
  }
  return {
    claude: createClaudeTurnExecutor(options),
    pi: createPiTurnExecutor(options),
    opencode: createOpenCodeTurnExecutor(options),
  };
}

/** Claude stream-json 单回合执行器。 */
function createClaudeTurnExecutor(
  options: AgentTurnExecutorFactoryOptions,
): AgentTurnExecutor {
  return async (input) => {
    const sink = createSignalSink(input.emitSignal);
    const workspace = await createAgentSessionWorkspace(options.workspaceRoot);
    let config: StagedClaudeMcpConfig | null = null;
    try {
      config = await stageClaudeMcpConfig({
        workDirectory: workspace.directory,
        url: input.mcp.endpoint,
        token: input.mcp.bearer,
      });
      const plan = buildAgentCliLaunchPlan({
        provider: "claude",
        executablePath: options.executables.claude,
        workDirectory: workspace.directory,
        sessionId: input.session.id,
        integrationPath: config.filePath,
      });
      await runTurnToSettlement({
        plan,
        timeoutMilliseconds: options.turnTimeoutMilliseconds,
        abortSignal: input.abortSignal,
        sink,
        start: async (handle) => {
          await handle.sendFrame(buildClaudeUserMessage(
            withHandoffPrefix(input.handoffContext, input.adapterInput.content),
          ));
        },
        onFrame: (frame, handle) => {
          for (const signal of parseClaudeStreamFrame(frame)) {
            if (signal.kind === "tool_permission_requested") {
              // argv 已预授权 mcp__lyra__*；到达权限请求的工具都在白名单外。
              void handle.sendFrame(buildClaudeToolPermissionResponse({
                controlRequestId: signal.controlRequestId,
                toolCallId: signal.toolCallId,
                decision: { behavior: "deny" },
              })).catch(() => undefined);
              continue;
            }
            routeWhitelistedSignal(signal, sink);
          }
        },
        dispose: async () => {
          await config?.dispose();
          await workspace.dispose();
        },
      });
    } catch (error: unknown) {
      await config?.dispose();
      await workspace.dispose();
      throw error;
    }
  };
}

/** 路由供应商信号中与统一信号同形的部分；工具请求与确认帧一律丢弃。 */
function routeWhitelistedSignal(
  signal: ClaudeStreamAdapterSignal | PiRpcAdapterSignal | OpenCodeAcpAdapterSignal,
  sink: TurnSignalSink,
  respond?: (response: Readonly<Record<string, unknown>>) => void,
): void {
  switch (signal.kind) {
    case "client_response_required":
      // 权限请求返回 cancelled，其余客户端能力统一禁用；仅 OpenCode 产生。
      respond?.(signal.response);
      return;
    case "tool_requested":
    case "tool_started":
    case "tool_completed":
    case "tool_permission_requested":
    case "command_accepted":
    case "control_accepted":
    case "ignored":
      // 工具调用经 MCP HTTP 独立执行并持久化；协议确认帧不产生 App 事件。
      return;
    default:
      sink.emit(signal);
  }
}

/** Pi RPC 单回合执行器。 */
function createPiTurnExecutor(
  options: AgentTurnExecutorFactoryOptions,
): AgentTurnExecutor {
  return async (input) => {
    const sink = createSignalSink(input.emitSignal);
    const workspace = await createAgentSessionWorkspace(options.workspaceRoot);
    try {
      const plan = buildAgentCliLaunchPlan({
        provider: "pi",
        executablePath: options.executables.pi,
        workDirectory: workspace.directory,
        sessionId: input.session.id,
        integrationPath: options.piExtensionPath,
        sessionDirectory: workspace.directory,
      });
      await runTurnToSettlement({
        plan,
        timeoutMilliseconds: options.turnTimeoutMilliseconds,
        abortSignal: input.abortSignal,
        sink,
        start: async (handle) => {
          await handle.sendFrame(
            buildPiPromptCommand(
              randomUUID(),
              withHandoffPrefix(input.handoffContext, input.adapterInput.content),
            ),
          );
        },
        onFrame: (frame) => {
          for (const signal of parsePiRpcFrame(frame)) {
            routeWhitelistedSignal(signal, sink);
          }
        },
        dispose: () => workspace.dispose(),
      });
    } catch (error: unknown) {
      await workspace.dispose();
      throw error;
    }
  };
}

/** OpenCode ACP 单回合执行器。 */
function createOpenCodeTurnExecutor(
  options: AgentTurnExecutorFactoryOptions,
): AgentTurnExecutor {
  return async (input) => {
    const sink = createSignalSink(input.emitSignal);
    const workspace = await createAgentSessionWorkspace(options.workspaceRoot);
    try {
      const plan = buildAgentCliLaunchPlan({
        provider: "opencode",
        executablePath: options.executables.opencode,
        workDirectory: workspace.directory,
        sessionId: input.session.id,
      });
      // 握手响应按关联 ID 等待；适配器建立前的帧先缓存，避免丢失早期通知。
      const pending = new Map<string, (frame: Record<string, unknown>) => void>();
      const earlyFrames: unknown[] = [];
      let adapterRoute: ((frame: unknown) => void) | null = null;
      const waitForResponse = (id: string): Promise<Record<string, unknown>> => new Promise(
        (resolve) => {
          pending.set(id, resolve);
        },
      );
      await runTurnToSettlement({
        plan,
        timeoutMilliseconds: options.turnTimeoutMilliseconds,
        abortSignal: input.abortSignal,
        sink,
        start: async (handle) => {
          const exitFailure = handle.completion.then(() => {
            throw new Error("OpenCode 进程在协议握手期间退出");
          });
          exitFailure.catch(() => undefined);

          const initializeId = `lyra-init-${randomUUID()}`;
          await handle.sendFrame(
            buildOpenCodeInitializeRequest(initializeId, options.clientVersion),
          );
          const initializeFrame = await Promise.race([
            waitForResponse(initializeId),
            exitFailure,
          ]);
          parseOpenCodeInitializeResponse(initializeFrame, initializeId);

          const newSessionId = `lyra-session-${randomUUID()}`;
          await handle.sendFrame(buildOpenCodeNewSessionRequest({
            requestId: newSessionId,
            workDirectory: workspace.directory,
            mcpServer: {
              type: "http",
              name: "lyra",
              url: input.mcp.endpoint,
              headers: [{
                name: "authorization",
                value: `Bearer ${input.mcp.bearer}`,
              }],
            },
          }));
          const newSessionFrame = await Promise.race([
            waitForResponse(newSessionId),
            exitFailure,
          ]);
          const openCodeSession = parseOpenCodeNewSessionResponse(
            newSessionFrame,
            newSessionId,
          );

          const promptId = `lyra-prompt-${randomUUID()}`;
          const adapter = new OpenCodeAcpTurnAdapter({
            sessionId: openCodeSession.sessionId,
            promptRequestId: promptId,
          });
          adapterRoute = (frame) => {
            for (const signal of adapter.parseFrame(frame)) {
              routeWhitelistedSignal(signal, sink, (response) => {
                void handle.sendFrame(response).catch(() => undefined);
              });
            }
          };
          for (const frame of earlyFrames.splice(0)) {
            adapterRoute(frame);
          }
          await handle.sendFrame(buildOpenCodePromptRequest({
            requestId: promptId,
            sessionId: openCodeSession.sessionId,
            content: withHandoffPrefix(input.handoffContext, input.adapterInput.content),
          }));
        },
        onFrame: (frame) => {
          const responseId = handshakeResponseId(frame);
          if (responseId !== null) {
            const resolver = pending.get(responseId);
            if (resolver !== undefined) {
              pending.delete(responseId);
              resolver(frame as Record<string, unknown>);
              return;
            }
          }
          if (adapterRoute === null) {
            earlyFrames.push(frame);
            return;
          }
          adapterRoute(frame);
        },
        dispose: () => workspace.dispose(),
      });
    } catch (error: unknown) {
      await workspace.dispose();
      throw error;
    }
  };
}

/** 识别握手响应帧的字符串关联 ID；其余帧返回 null。 */
function handshakeResponseId(frame: unknown): string | null {
  if (typeof frame !== "object" || frame === null || Array.isArray(frame)) {
    return null;
  }
  const id = (frame as Record<string, unknown>).id;
  return typeof id === "string" && id.length > 0 ? id : null;
}

/** 切换后首个回合把一次性 handoff 上下文并入用户 prompt 前缀。 */
function withHandoffPrefix(handoffContext: string | null, content: string): string {
  return handoffContext === null ? content : `${handoffContext}\n\n${content}`;
}
