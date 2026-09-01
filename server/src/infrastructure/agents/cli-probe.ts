import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";

import type { AgentProvider } from "../../domain/agent/index.js";

/** 可用性探测不会向客户端公开的稳定失败原因。 */
export type AgentCliProbeFailure =
  | "EXECUTABLE_NOT_FOUND"
  | "PROCESS_TIMEOUT"
  | "OUTPUT_LIMIT_EXCEEDED"
  | "NON_ZERO_EXIT"
  | "VERSION_UNRECOGNIZED"
  | "VERSION_NOT_ALLOWED";

/** 启动一次 CLI 版本探测所需的最小配置。 */
export interface AgentCliProbeInput {
  provider: AgentProvider;
  executablePath: string;
  versionArguments: readonly string[];
  /** 经适配器契约验证的精确版本白名单，不使用模糊范围猜测兼容性。 */
  allowedVersions: readonly string[];
  timeoutMilliseconds: number;
  /** 仅用于解析基于 `/usr/bin/env` 的脚本解释器，不接受其他环境变量。 */
  pathEnvironment?: string;
}

/** Agent CLI 探测结果；失败时不包含 stderr、路径或宿主环境信息。 */
export type AgentCliProbeResult =
  | { provider: AgentProvider; status: "available"; version: string }
  | { provider: AgentProvider; status: "unavailable"; reason: AgentCliProbeFailure };

/** 单次版本探测允许读取的 stdout/stderr 总上限。 */
const MAX_PROBE_OUTPUT_BYTES = 16 * 1024;

/**
 * 在独立空目录中安全探测 Agent CLI 版本。
 *
 * 探测始终使用绝对可执行路径、`shell: false`、受限环境和硬超时；不会传入
 * prompt、项目目录或认证环境变量，因此不会启动模型会话。
 *
 * @param input - 供应商、可执行文件、版本参数和超时。
 * @returns 可用版本，或不含敏感上下文的稳定失败原因。
 */
export async function probeAgentCli(input: AgentCliProbeInput): Promise<AgentCliProbeResult> {
  if (!isAbsolute(input.executablePath)) {
    throw new RangeError("Agent CLI 必须配置绝对可执行路径");
  }
  if (!Number.isSafeInteger(input.timeoutMilliseconds) || input.timeoutMilliseconds <= 0) {
    throw new RangeError("Agent CLI 探测超时必须是正安全整数毫秒数");
  }
  const allowedVersions = normalizeAllowedVersions(input.allowedVersions);

  const workDirectory = await mkdtemp(join(tmpdir(), "lyra-agent-probe-"));
  try {
    const processResult = await runVersionProcess(input, workDirectory);
    if (processResult.failure !== null) {
      return { provider: input.provider, status: "unavailable", reason: processResult.failure };
    }
    const match = /\bv?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)/.exec(processResult.output);
    if (match?.[1] === undefined) {
      return {
        provider: input.provider,
        status: "unavailable",
        reason: "VERSION_UNRECOGNIZED",
      };
    }
    const version = match[1];
    if (!allowedVersions.has(version)) {
      return {
        provider: input.provider,
        status: "unavailable",
        reason: "VERSION_NOT_ALLOWED",
      };
    }
    return { provider: input.provider, status: "available", version };
  } finally {
    // 目录由本函数唯一创建；清理失败属于运维错误，必须向启动方显式抛出。
    await rm(workDirectory, { recursive: true });
  }
}

/** 规范并校验管理员配置的精确版本白名单。 */
function normalizeAllowedVersions(versions: readonly string[]): ReadonlySet<string> {
  if (versions.length === 0) {
    throw new RangeError("Agent CLI 允许版本不能为空");
  }
  const normalized = versions.map((version) => version.trim());
  if (normalized.some((version) => !/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(version))) {
    throw new RangeError("Agent CLI 允许版本必须是完整版本号");
  }
  return new Set(normalized);
}

/** 子进程执行完成后的内部结果。 */
interface VersionProcessResult {
  output: string;
  failure: AgentCliProbeFailure | null;
}

/** 使用无 shell 子进程收集受限版本输出。 */
function runVersionProcess(
  input: AgentCliProbeInput,
  workDirectory: string,
): Promise<VersionProcessResult> {
  return new Promise((resolve, reject) => {
    const environment: NodeJS.ProcessEnv = {};
    if (input.pathEnvironment !== undefined) {
      environment.PATH = input.pathEnvironment;
    }
    const child = spawn(input.executablePath, [...input.versionArguments], {
      cwd: workDirectory,
      env: environment,
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let output = "";
    let settled = false;
    let outputLimitExceeded = false;

    const finish = (result: VersionProcessResult): void => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timeout);
      resolve(result);
    };
    const appendOutput = (chunk: Buffer): void => {
      if (outputLimitExceeded) {
        return;
      }
      output += chunk.toString("utf8");
      if (Buffer.byteLength(output, "utf8") > MAX_PROBE_OUTPUT_BYTES) {
        outputLimitExceeded = true;
        child.kill("SIGKILL");
      }
    };

    child.stdout.on("data", appendOutput);
    child.stderr.on("data", appendOutput);
    child.once("error", (error: NodeJS.ErrnoException) => {
      if (error.code === "ENOENT") {
        finish({ output: "", failure: "EXECUTABLE_NOT_FOUND" });
        return;
      }
      clearTimeout(timeout);
      reject(error);
    });
    child.once("close", (code) => {
      if (outputLimitExceeded) {
        finish({ output: "", failure: "OUTPUT_LIMIT_EXCEEDED" });
        return;
      }
      finish({ output, failure: code === 0 ? null : "NON_ZERO_EXIT" });
    });

    const timeout = setTimeout(() => {
      child.kill("SIGKILL");
      finish({ output: "", failure: "PROCESS_TIMEOUT" });
    }, input.timeoutMilliseconds);
    timeout.unref();
  });
}
