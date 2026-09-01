import { isAbsolute } from "node:path";

import type { AgentProvider } from "../../domain/agent/index.js";
import { CLAUDE_LYRA_MCP_TOOL_PATTERN } from "./claude-stream-adapter.js";

/** 三个 CLI 在 Lyra 中使用的机器可读协议。 */
export type AgentCliProtocol = "claude_stream_json" | "pi_rpc" | "opencode_acp";

/** 不含 prompt 或凭据、可直接交给受限子进程运行器的启动计划。 */
export interface AgentCliLaunchPlan {
  provider: AgentProvider;
  protocol: AgentCliProtocol;
  executablePath: string;
  arguments: readonly string[];
  workDirectory: string;
  stdio: "json_lines";
  /** 运行器只能注入服务端显式配置的凭据，不得继承整个宿主环境。 */
  environmentPolicy: "explicit_credentials_only";
}

/**
 * 按本机已验证的 CLI 协议生成最小权限启动参数。
 *
 * Claude 只启用显式 Lyra MCP；Pi 关闭内建工具和自动发现，只加载 Lyra 扩展；
 * OpenCode 以 pure ACP 在空目录运行，具体工具权限由 ACP 客户端拒绝或转发。
 * prompt 始终通过标准输入协议发送，不能出现在 argv、进程列表或审计日志中。
 *
 * @param input - 供应商、绝对路径、会话 ID、可选模型与适配集成文件。
 * @returns 无 shell、无 prompt 的确定性 CLI 启动计划。
 */
export function buildAgentCliLaunchPlan(input: {
  provider: AgentProvider;
  executablePath: string;
  workDirectory: string;
  sessionId: string;
  model?: string;
  /** Claude 使用 MCP JSON，Pi 使用显式扩展；OpenCode ACP 不接受该字段。 */
  integrationPath?: string;
  /** Pi 原生会话文件只能写入服务端指定的隔离目录。 */
  sessionDirectory?: string;
}): AgentCliLaunchPlan {
  requireAbsolutePath(input.executablePath, "Agent CLI");
  requireAbsolutePath(input.workDirectory, "Agent 工作目录");
  const sessionId = requireText(input.sessionId, "Agent 会话 ID");
  const model = input.model === undefined ? null : requireText(input.model, "Agent 模型");

  switch (input.provider) {
    case "claude": {
      const integrationPath = requireOptionalAbsolutePath(
        input.integrationPath,
        "Claude MCP 配置",
      );
      const arguments_: string[] = [
        "--print",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--bare",
        "--disable-slash-commands",
        "--no-chrome",
        "--permission-mode", "dontAsk",
        "--tools", "",
        "--allowedTools", CLAUDE_LYRA_MCP_TOOL_PATTERN,
        "--strict-mcp-config",
        "--mcp-config", integrationPath,
        "--session-id", sessionId,
      ];
      appendModel(arguments_, model);
      return plan(input, "claude_stream_json", arguments_);
    }
    case "pi": {
      const integrationPath = requireOptionalAbsolutePath(
        input.integrationPath,
        "Pi Lyra 扩展",
      );
      const sessionDirectory = requireOptionalAbsolutePath(
        input.sessionDirectory,
        "Pi 会话目录",
      );
      const arguments_: string[] = [
        "--mode", "rpc",
        "--session-id", sessionId,
        "--session-dir", sessionDirectory,
        "--no-builtin-tools",
        "--no-extensions",
        "--extension", integrationPath,
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-approve",
      ];
      appendModel(arguments_, model);
      return plan(input, "pi_rpc", arguments_);
    }
    case "opencode": {
      if (input.integrationPath !== undefined || input.sessionDirectory !== undefined) {
        throw new RangeError("OpenCode ACP 的工具和会话由协议管理，不能传入集成或会话目录参数");
      }
      return plan(input, "opencode_acp", [
        "acp",
        "--pure",
        "--cwd", input.workDirectory,
        "--log-level", "ERROR",
      ]);
    }
  }
}

/** 构造三个供应商共享的受限进程字段。 */
function plan(
  input: { provider: AgentProvider; executablePath: string; workDirectory: string },
  protocol: AgentCliProtocol,
  arguments_: readonly string[],
): AgentCliLaunchPlan {
  return {
    provider: input.provider,
    protocol,
    executablePath: input.executablePath,
    arguments: arguments_,
    workDirectory: input.workDirectory,
    stdio: "json_lines",
    environmentPolicy: "explicit_credentials_only",
  };
}

/** 供应商支持 argv 模型覆盖时追加成独立参数，避免 shell 拼接。 */
function appendModel(arguments_: string[], model: string | null): void {
  if (model !== null) {
    arguments_.push("--model", model);
  }
}

/** 校验必填绝对路径，不检查存在性；存在性由启动器的稳定错误路径处理。 */
function requireAbsolutePath(value: string, label: string): string {
  if (!isAbsolute(value)) {
    throw new RangeError(`${label}必须是绝对路径`);
  }
  return value;
}

/** 校验供应商必需的集成路径。 */
function requireOptionalAbsolutePath(value: string | undefined, label: string): string {
  if (value === undefined) {
    throw new RangeError(`${label}不能为空`);
  }
  return requireAbsolutePath(value, label);
}

/** 去除边界空白并拒绝空协议标识。 */
function requireText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}
