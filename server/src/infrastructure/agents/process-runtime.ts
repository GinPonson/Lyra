import { spawn } from "node:child_process";
import { isAbsolute } from "node:path";
import { StringDecoder } from "node:string_decoder";

import type { AgentCliLaunchPlan } from "./launch-plan.js";

/** 受控 CLI 运行时对上层暴露的稳定失败分类。 */
export type AgentCliRuntimeFailure =
  | "EXECUTABLE_NOT_FOUND"
  | "PROCESS_START_FAILED"
  | "PROCESS_TIMEOUT"
  | "OUTPUT_LIMIT_EXCEEDED"
  | "INVALID_PROTOCOL_FRAME"
  | "FRAME_HANDLER_FAILED"
  | "NON_ZERO_EXIT"
  | "CANCELLED";

/** 进程终态不包含 stderr、宿主路径、prompt 或供应商原始错误。 */
export type AgentCliRuntimeResult =
  | {
      status: "completed";
      framesReceived: number;
      stderrBytes: number;
    }
  | {
      status: "failed";
      reason: AgentCliRuntimeFailure;
      framesReceived: number;
      stderrBytes: number;
    };

/** 运行中的 JSON Lines CLI；调用方负责按供应商协议发送合法帧。 */
export interface AgentCliProcessHandle {
  /** 把单个 JSON 帧写入 stdin；prompt 不会进入 argv。 */
  sendFrame(frame: unknown): Promise<void>;
  /** 声明当前协议不会再发送数据，让一次性 CLI 可以自然退出。 */
  finishInput(): void;
  /** 请求终止当前回合；超出宽限期后会强制结束。 */
  cancel(): void;
  /** 进程的脱敏终态。 */
  completion: Promise<AgentCliRuntimeResult>;
}

/** 允许注入子进程的最小凭据环境；禁止 NODE_OPTIONS、HOME 等行为变量。 */
const ALLOWED_ENVIRONMENT_KEYS = new Set([
  "PATH",
  "ANTHROPIC_API_KEY",
  "CLAUDE_CODE_OAUTH_TOKEN",
  "OPENAI_API_KEY",
  "OPENROUTER_API_KEY",
  "GOOGLE_API_KEY",
  "GEMINI_API_KEY",
  "MISTRAL_API_KEY",
  "GROQ_API_KEY",
  "XAI_API_KEY",
]);

const MAX_INPUT_FRAME_BYTES = 1024 * 1024;
const DEFAULT_MAXIMUM_OUTPUT_BYTES = 4 * 1024 * 1024;
const DEFAULT_CANCEL_GRACE_MILLISECONDS = 1_000;

/**
 * 启动一个无 shell、无宿主环境继承的持久 JSON Lines 子进程。
 *
 * 运行时只负责进程、字节边界、JSON 帧和取消，不解释 Claude/Pi/OpenCode 的
 * 供应商语义。stdout 逐帧交给上层适配器，stderr 只计数不返回，避免把令牌、
 * 路径或 prompt 复制到客户端及持久化层。
 *
 * @param input - 已验证启动计划、显式凭据、硬超时、输出上限和同步帧处理器。
 * @returns 可发送多帧并显式结束或取消的运行句柄。
 */
export function startAgentCliProcess(input: {
  plan: AgentCliLaunchPlan;
  environment?: Readonly<Record<string, string>>;
  timeoutMilliseconds: number;
  maximumOutputBytes?: number;
  cancelGraceMilliseconds?: number;
  onFrame: (frame: unknown) => void;
}): AgentCliProcessHandle {
  validateRuntimeInput(input);
  const maximumOutputBytes = input.maximumOutputBytes ?? DEFAULT_MAXIMUM_OUTPUT_BYTES;
  const cancelGraceMilliseconds = input.cancelGraceMilliseconds
    ?? DEFAULT_CANCEL_GRACE_MILLISECONDS;
  const environment = normalizeEnvironment(input.environment ?? {});

  let resolveCompletion: (result: AgentCliRuntimeResult) => void = () => undefined;
  const completion = new Promise<AgentCliRuntimeResult>((resolve) => {
    resolveCompletion = resolve;
  });
  const child = spawn(input.plan.executablePath, [...input.plan.arguments], {
    cwd: input.plan.workDirectory,
    env: environment,
    shell: false,
    stdio: ["pipe", "pipe", "pipe"],
  });
  const decoder = new StringDecoder("utf8");
  let stdoutBuffer = "";
  let totalOutputBytes = 0;
  let stderrBytes = 0;
  let framesReceived = 0;
  let failure: AgentCliRuntimeFailure | null = null;
  let settled = false;
  let forceKillTimer: NodeJS.Timeout | null = null;

  const settle = (result: AgentCliRuntimeResult): void => {
    if (settled) {
      return;
    }
    settled = true;
    clearTimeout(processTimeout);
    if (forceKillTimer !== null) {
      clearTimeout(forceKillTimer);
    }
    resolveCompletion(result);
  };
  const failAndKill = (reason: AgentCliRuntimeFailure, signal: NodeJS.Signals): void => {
    if (failure === null) {
      failure = reason;
      child.kill(signal);
    }
  };
  const consumeLine = (line: string): void => {
    const normalized = line.endsWith("\r") ? line.slice(0, -1) : line;
    if (normalized.length === 0 || failure !== null) {
      return;
    }
    let frame: unknown;
    try {
      frame = JSON.parse(normalized) as unknown;
    } catch {
      failAndKill("INVALID_PROTOCOL_FRAME", "SIGKILL");
      return;
    }
    try {
      input.onFrame(frame);
      framesReceived += 1;
    } catch {
      failAndKill("FRAME_HANDLER_FAILED", "SIGKILL");
    }
  };
  const drainLines = (): void => {
    let newlineIndex = stdoutBuffer.indexOf("\n");
    while (newlineIndex >= 0) {
      const line = stdoutBuffer.slice(0, newlineIndex);
      stdoutBuffer = stdoutBuffer.slice(newlineIndex + 1);
      consumeLine(line);
      newlineIndex = stdoutBuffer.indexOf("\n");
    }
  };
  const accountOutput = (bytes: number): boolean => {
    totalOutputBytes += bytes;
    if (totalOutputBytes > maximumOutputBytes) {
      failAndKill("OUTPUT_LIMIT_EXCEEDED", "SIGKILL");
      return false;
    }
    return true;
  };

  child.stdout.on("data", (chunk: Buffer) => {
    if (!accountOutput(chunk.length) || failure !== null) {
      return;
    }
    stdoutBuffer += decoder.write(chunk);
    drainLines();
  });
  child.stderr.on("data", (chunk: Buffer) => {
    stderrBytes += chunk.length;
    accountOutput(chunk.length);
  });
  child.once("error", (error: NodeJS.ErrnoException) => {
    failure = error.code === "ENOENT"
      ? "EXECUTABLE_NOT_FOUND"
      : "PROCESS_START_FAILED";
    settle({ status: "failed", reason: failure, framesReceived, stderrBytes });
  });
  child.once("close", (code) => {
    if (failure === null) {
      stdoutBuffer += decoder.end();
      if (stdoutBuffer.length > 0) {
        consumeLine(stdoutBuffer);
        stdoutBuffer = "";
      }
    }
    if (failure !== null) {
      settle({ status: "failed", reason: failure, framesReceived, stderrBytes });
      return;
    }
    if (code !== 0) {
      settle({
        status: "failed",
        reason: "NON_ZERO_EXIT",
        framesReceived,
        stderrBytes,
      });
      return;
    }
    settle({ status: "completed", framesReceived, stderrBytes });
  });

  const processTimeout = setTimeout(() => {
    failAndKill("PROCESS_TIMEOUT", "SIGKILL");
  }, input.timeoutMilliseconds);
  processTimeout.unref();

  return {
    async sendFrame(frame: unknown): Promise<void> {
      if (settled || child.stdin.destroyed || child.stdin.writableEnded) {
        throw new Error("Agent CLI stdin 已关闭");
      }
      const json = JSON.stringify(frame);
      if (json === undefined) {
        throw new TypeError("Agent CLI 输入必须可序列化为 JSON");
      }
      const serialized = `${json}\n`;
      if (Buffer.byteLength(serialized, "utf8") > MAX_INPUT_FRAME_BYTES) {
        throw new RangeError("Agent CLI 单个输入帧超过 1 MiB");
      }
      await new Promise<void>((resolve, reject) => {
        child.stdin.write(serialized, "utf8", (error) => {
          if (error === null || error === undefined) {
            resolve();
            return;
          }
          reject(new Error("Agent CLI 输入写入失败", { cause: error }));
        });
      });
    },
    finishInput(): void {
      if (!child.stdin.destroyed && !child.stdin.writableEnded) {
        child.stdin.end();
      }
    },
    cancel(): void {
      if (settled || failure !== null) {
        return;
      }
      failure = "CANCELLED";
      child.kill("SIGTERM");
      forceKillTimer = setTimeout(() => {
        child.kill("SIGKILL");
      }, cancelGraceMilliseconds);
      forceKillTimer.unref();
    },
    completion,
  };
}

/** 验证资源上限和启动计划边界。 */
function validateRuntimeInput(input: {
  plan: AgentCliLaunchPlan;
  timeoutMilliseconds: number;
  maximumOutputBytes?: number;
  cancelGraceMilliseconds?: number;
}): void {
  if (!isAbsolute(input.plan.executablePath) || !isAbsolute(input.plan.workDirectory)) {
    throw new RangeError("Agent CLI 运行路径必须是绝对路径");
  }
  requirePositiveInteger(input.timeoutMilliseconds, "Agent CLI 运行超时");
  requirePositiveInteger(
    input.maximumOutputBytes ?? DEFAULT_MAXIMUM_OUTPUT_BYTES,
    "Agent CLI 输出上限",
  );
  requirePositiveInteger(
    input.cancelGraceMilliseconds ?? DEFAULT_CANCEL_GRACE_MILLISECONDS,
    "Agent CLI 取消宽限期",
  );
}

/** 只复制显式允许的凭据变量，不继承宿主环境。 */
function normalizeEnvironment(
  input: Readonly<Record<string, string>>,
): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(input)) {
    if (!ALLOWED_ENVIRONMENT_KEYS.has(key)) {
      throw new RangeError(`Agent CLI 禁止注入环境变量: ${key}`);
    }
    if (value.length === 0) {
      throw new RangeError(`Agent CLI 环境变量不能为空: ${key}`);
    }
    environment[key] = value;
  }
  return environment;
}

/** 验证毫秒数或字节数为正安全整数。 */
function requirePositiveInteger(value: number, label: string): void {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new RangeError(`${label}必须是正安全整数`);
  }
}
