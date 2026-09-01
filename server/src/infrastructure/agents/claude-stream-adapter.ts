import {
  AgentProtocolError,
  FINANCIAL_TOOL_NAMES,
} from "../../domain/agent/index.js";
import type { FinancialToolName } from "../../domain/agent/index.js";

/** Claude 侧唯一允许自动调用的 Lyra MCP 工具命名空间。 */
export const CLAUDE_LYRA_MCP_TOOL_PATTERN = "mcp__lyra__*";

/** Claude stream-json 标准用户输入帧。 */
export interface ClaudeUserMessage {
  type: "user";
  session_id: "";
  message: {
    role: "user";
    content: string;
  };
  parent_tool_use_id: null;
}

/** Claude SDK control protocol 的中断请求。 */
export interface ClaudeInterruptRequest {
  type: "control_request";
  request_id: string;
  request: {
    subtype: "interrupt";
  };
}

/** Lyra 对 Claude 工具授权请求返回的 SDK control response。 */
export type ClaudeToolPermissionResponse = {
  type: "control_response";
  response: {
    subtype: "success";
    request_id: string;
    response:
      | {
          behavior: "allow";
          updatedInput: Readonly<Record<string, unknown>>;
          toolUseID: string;
        }
      | {
          behavior: "deny";
          message: string;
          toolUseID: string;
        };
  };
};

/** Claude stream-json 帧转换后的供应商无关信号。 */
export type ClaudeStreamAdapterSignal =
  | { kind: "message_delta"; delta: string }
  | { kind: "reasoning_delta"; delta: string }
  | { kind: "message_snapshot"; content: string; stopReason: string }
  | { kind: "message_failed"; outcome: "failed" }
  | {
      kind: "tool_requested";
      toolCallId: string;
      providerToolName: string;
      toolName: FinancialToolName | null;
      arguments: Readonly<Record<string, unknown>>;
    }
  | {
      kind: "tool_permission_requested";
      controlRequestId: string;
      toolCallId: string;
      providerToolName: string;
      toolName: FinancialToolName | null;
      arguments: Readonly<Record<string, unknown>>;
    }
  | { kind: "control_accepted"; requestId: string }
  | { kind: "usage_updated"; totalTokens: number; totalCost: number }
  | { kind: "turn_settled" }
  | { kind: "ignored"; sourceType: string };

/** Claude 协议异常；供应商原始错误只保留在服务端诊断字段。 */
export class ClaudeStreamProtocolError extends AgentProtocolError {
  /** @param internalReason - 协议版本、非法帧或供应商错误的内部定位信息。 */
  constructor(internalReason: string) {
    super("claude", internalReason);
    this.name = "ClaudeStreamProtocolError";
  }
}

const LYRA_MCP_TOOL_PREFIX = "mcp__lyra__";
const KNOWN_SYSTEM_SUBTYPES = new Set([
  "init",
  "compact_boundary",
  "status",
  "post_turn_summary",
  "api_retry",
  "local_command_output",
  "hook_started",
  "hook_progress",
  "hook_response",
  "files_persisted",
  "task_notification",
  "task_started",
  "task_progress",
  "session_state_changed",
  "elicitation_complete",
]);
const KNOWN_IGNORED_FRAME_TYPES = new Set([
  "user",
  "rate_limit_event",
  "tool_progress",
  "auth_status",
  "tool_use_summary",
  "prompt_suggestion",
  "keep_alive",
  "control_cancel_request",
]);

/** 构造官方 stream-json 约定的用户消息；会话身份由启动参数固定。 */
export function buildClaudeUserMessage(content: string): ClaudeUserMessage {
  return {
    type: "user",
    session_id: "",
    message: {
      role: "user",
      content: requireInputText(content, "Claude 用户消息"),
    },
    parent_tool_use_id: null,
  };
}

/** 构造可关联响应的 Claude 中断控制帧。 */
export function buildClaudeInterruptRequest(requestId: string): ClaudeInterruptRequest {
  return {
    type: "control_request",
    request_id: requireInputText(requestId, "Claude control request ID"),
    request: { subtype: "interrupt" },
  };
}

/**
 * 构造 Claude 工具权限响应。
 *
 * allow 必须回传经过服务端校验的参数副本；deny 使用固定消息，避免把内部策略
 * 或底层异常暴露给模型。该函数只编码协议，不替代 FinancialToolRegistry 授权。
 */
export function buildClaudeToolPermissionResponse(input: {
  controlRequestId: string;
  toolCallId: string;
  decision:
    | { behavior: "allow"; updatedInput: Readonly<Record<string, unknown>> }
    | { behavior: "deny" };
}): ClaudeToolPermissionResponse {
  const requestId = requireInputText(input.controlRequestId, "Claude control request ID");
  const toolCallId = requireInputText(input.toolCallId, "Claude tool use ID");
  const response = input.decision.behavior === "allow"
    ? {
        behavior: "allow" as const,
        updatedInput: { ...input.decision.updatedInput },
        toolUseID: toolCallId,
      }
    : {
        behavior: "deny" as const,
        message: "Lyra 只允许已授权的只读金融工具",
        toolUseID: toolCallId,
      };
  return {
    type: "control_response",
    response: {
      subtype: "success",
      request_id: requestId,
      response,
    },
  };
}

/**
 * 把 Claude Code 2.1.x stream-json 帧转换为 Lyra 编排器信号。
 *
 * partial text/thinking 仅用于实时展示；最终可见文本以 result.result 为权威
 * 快照。工具参数只停留在运行时信号中，持久化必须继续使用既有安全摘要计划。
 * 已知但当前不消费的生命周期帧被明确标记 ignored，未知协议则立即失败。
 */
export function parseClaudeStreamFrame(frame: unknown): readonly ClaudeStreamAdapterSignal[] {
  const record = requireRecord(frame, "Claude stream-json 帧");
  const type = requireTextValue(record.type, "Claude 帧类型");
  switch (type) {
    case "stream_event":
      return parseStreamEvent(record);
    case "assistant":
      return parseAssistantMessage(record);
    case "result":
      return parseResult(record);
    case "control_request":
      return parseControlRequest(record);
    case "control_response":
      return parseControlResponse(record);
    case "system":
      return parseSystemMessage(record);
    default:
      if (KNOWN_IGNORED_FRAME_TYPES.has(type)) {
        return [{ kind: "ignored", sourceType: type }];
      }
      throw new ClaudeStreamProtocolError(`未知 Claude stream-json 帧类型: ${type}`);
  }
}

/** 解析 Anthropic 原始流事件；不拼接工具参数 JSON 或持久化 thinking。 */
function parseStreamEvent(
  record: Readonly<Record<string, unknown>>,
): readonly ClaudeStreamAdapterSignal[] {
  const parentToolUseId = record.parent_tool_use_id;
  if (parentToolUseId !== null && parentToolUseId !== undefined) {
    throw new ClaudeStreamProtocolError("Claude 返回了禁止的嵌套 Agent 流事件");
  }
  const event = requireRecord(record.event, "Claude 原始流事件");
  const eventType = requireTextValue(event.type, "Claude 原始流事件类型");
  if (eventType === "content_block_delta") {
    const delta = requireRecord(event.delta, "Claude 内容增量");
    const deltaType = requireTextValue(delta.type, "Claude 内容增量类型");
    switch (deltaType) {
      case "text_delta": {
        const text = requireString(delta.text, "Claude 文本增量");
        return text.length === 0 ? [] : [{ kind: "message_delta", delta: text }];
      }
      case "thinking_delta": {
        const thinking = requireString(delta.thinking, "Claude reasoning 增量");
        return thinking.length === 0 ? [] : [{ kind: "reasoning_delta", delta: thinking }];
      }
      case "input_json_delta":
      case "signature_delta":
      case "citations_delta":
        return [{ kind: "ignored", sourceType: `stream_event:${deltaType}` }];
      default:
        throw new ClaudeStreamProtocolError(`未知 Claude 内容增量类型: ${deltaType}`);
    }
  }
  switch (eventType) {
    case "message_start":
    case "content_block_start":
    case "content_block_stop":
    case "message_delta":
    case "message_stop":
    case "ping":
      return [{ kind: "ignored", sourceType: `stream_event:${eventType}` }];
    case "error":
      return [{ kind: "message_failed", outcome: "failed" }];
    default:
      throw new ClaudeStreamProtocolError(`未知 Claude 原始流事件: ${eventType}`);
  }
}

/** 从完整 assistant 消息中提取工具声明；最终文本统一由 result 帧收口。 */
function parseAssistantMessage(
  record: Readonly<Record<string, unknown>>,
): readonly ClaudeStreamAdapterSignal[] {
  if (record.error !== undefined) {
    requireTextValue(record.error, "Claude assistant 错误分类");
    return [{ kind: "message_failed", outcome: "failed" }];
  }
  const message = requireRecord(record.message, "Claude assistant 消息");
  if (message.role !== "assistant") {
    throw new ClaudeStreamProtocolError("Claude assistant 帧的消息角色无效");
  }
  if (!Array.isArray(message.content)) {
    throw new ClaudeStreamProtocolError("Claude assistant content 不是数组");
  }
  const signals: ClaudeStreamAdapterSignal[] = [];
  for (const value of message.content) {
    const block = requireRecord(value, "Claude assistant 内容块");
    const blockType = requireTextValue(block.type, "Claude assistant 内容块类型");
    switch (blockType) {
      case "text":
        requireString(block.text, "Claude assistant 文本");
        break;
      case "thinking":
        requireString(block.thinking, "Claude assistant thinking");
        break;
      case "redacted_thinking":
        break;
      case "tool_use":
        signals.push(parseToolRequest(block));
        break;
      default:
        throw new ClaudeStreamProtocolError(`禁止或未知的 Claude assistant 内容块: ${blockType}`);
    }
  }
  return signals.length === 0
    ? [{ kind: "ignored", sourceType: "assistant" }]
    : signals;
}

/** 把完整 tool_use 块映射为可交给统一工具策略层的请求。 */
function parseToolRequest(
  block: Readonly<Record<string, unknown>>,
): ClaudeStreamAdapterSignal {
  const providerToolName = requireTextValue(block.name, "Claude 工具名");
  return {
    kind: "tool_requested",
    toolCallId: requireTextValue(block.id, "Claude tool use ID"),
    providerToolName,
    toolName: resolveLyraToolName(providerToolName),
    arguments: requireRecord(block.input, "Claude 工具参数"),
  };
}

/** result 是单个逻辑回合的权威文本、累计 usage 与 settled 边界。 */
function parseResult(record: Readonly<Record<string, unknown>>): readonly ClaudeStreamAdapterSignal[] {
  const subtype = requireTextValue(record.subtype, "Claude result subtype");
  const usage = parseResultUsage(record);
  if (subtype === "success") {
    if (record.is_error !== false) {
      throw new ClaudeStreamProtocolError("Claude success result 的 is_error 不是 false");
    }
    const stopReason = record.stop_reason === null
      ? "end_turn"
      : requireTextValue(record.stop_reason, "Claude stop reason");
    return [
      {
        kind: "message_snapshot",
        content: requireString(record.result, "Claude 最终结果"),
        stopReason,
      },
      usage,
      { kind: "turn_settled" },
    ];
  }
  if (![
    "error_during_execution",
    "error_max_turns",
    "error_max_budget_usd",
    "error_max_structured_output_retries",
  ].includes(subtype)) {
    throw new ClaudeStreamProtocolError(`未知 Claude result subtype: ${subtype}`);
  }
  if (record.is_error !== true) {
    throw new ClaudeStreamProtocolError("Claude error result 的 is_error 不是 true");
  }
  return [usage, { kind: "message_failed", outcome: "failed" }, { kind: "turn_settled" }];
}

/** 将 Claude 累计 usage 缩减为 UI 所需总 token 和美元成本。 */
function parseResultUsage(
  record: Readonly<Record<string, unknown>>,
): Extract<ClaudeStreamAdapterSignal, { kind: "usage_updated" }> {
  const usage = requireRecord(record.usage, "Claude result usage");
  const totalTokens = [
    requireNonNegativeNumber(usage.input_tokens, "Claude input tokens"),
    requireNonNegativeNumber(usage.output_tokens, "Claude output tokens"),
    requireNonNegativeNumber(usage.cache_creation_input_tokens, "Claude cache creation tokens"),
    requireNonNegativeNumber(usage.cache_read_input_tokens, "Claude cache read tokens"),
  ].reduce((total, value) => total + value, 0);
  return {
    kind: "usage_updated",
    totalTokens,
    totalCost: requireNonNegativeNumber(record.total_cost_usd, "Claude total cost"),
  };
}

/** 只接受工具权限请求；MCP elicitation 和运行时变更均不属于 Lyra 第一阶段能力。 */
function parseControlRequest(
  record: Readonly<Record<string, unknown>>,
): readonly ClaudeStreamAdapterSignal[] {
  const request = requireRecord(record.request, "Claude control request");
  const subtype = requireTextValue(request.subtype, "Claude control request subtype");
  if (subtype !== "can_use_tool") {
    throw new ClaudeStreamProtocolError(`禁止的 Claude control request: ${subtype}`);
  }
  const providerToolName = requireTextValue(request.tool_name, "Claude 权限工具名");
  return [{
    kind: "tool_permission_requested",
    controlRequestId: requireTextValue(record.request_id, "Claude control request ID"),
    toolCallId: requireTextValue(request.tool_use_id, "Claude 权限 tool use ID"),
    providerToolName,
    toolName: resolveLyraToolName(providerToolName),
    arguments: requireRecord(request.input, "Claude 权限工具参数"),
  }];
}

/** 解析 Lyra 主动发出的 interrupt 等控制请求的关联结果。 */
function parseControlResponse(
  record: Readonly<Record<string, unknown>>,
): readonly ClaudeStreamAdapterSignal[] {
  const response = requireRecord(record.response, "Claude control response");
  const subtype = requireTextValue(response.subtype, "Claude control response subtype");
  const requestId = requireTextValue(response.request_id, "Claude control response request ID");
  if (subtype === "success") {
    return [{ kind: "control_accepted", requestId }];
  }
  if (subtype === "error") {
    const internalError = typeof response.error === "string" ? response.error : "missing error";
    throw new ClaudeStreamProtocolError(`Claude control request 被拒绝: ${internalError}`);
  }
  throw new ClaudeStreamProtocolError(`未知 Claude control response subtype: ${subtype}`);
}

/** 明确识别当前 Claude SDK 的系统生命周期消息。 */
function parseSystemMessage(
  record: Readonly<Record<string, unknown>>,
): readonly ClaudeStreamAdapterSignal[] {
  const subtype = requireTextValue(record.subtype, "Claude system subtype");
  if (!KNOWN_SYSTEM_SUBTYPES.has(subtype)) {
    throw new ClaudeStreamProtocolError(`未知 Claude system subtype: ${subtype}`);
  }
  return [{ kind: "ignored", sourceType: `system:${subtype}` }];
}

/** 仅把 Lyra MCP 命名空间且位于金融白名单中的工具解析为可执行工具。 */
function resolveLyraToolName(providerToolName: string): FinancialToolName | null {
  if (!providerToolName.startsWith(LYRA_MCP_TOOL_PREFIX)) {
    return null;
  }
  const toolName = providerToolName.slice(LYRA_MCP_TOOL_PREFIX.length);
  return (FINANCIAL_TOOL_NAMES as readonly string[]).includes(toolName)
    ? toolName as FinancialToolName
    : null;
}

/** 收窄 JSON object，数组不视为对象。 */
function requireRecord(value: unknown, label: string): Readonly<Record<string, unknown>> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ClaudeStreamProtocolError(`${label}不是对象`);
  }
  return value as Readonly<Record<string, unknown>>;
}

/** 接受协议中的可为空字符串。 */
function requireString(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new ClaudeStreamProtocolError(`${label}不是字符串`);
  }
  return value;
}

/** 接受非空协议文本。 */
function requireTextValue(value: unknown, label: string): string {
  const text = requireString(value, label);
  if (text.length === 0) {
    throw new ClaudeStreamProtocolError(`${label}为空`);
  }
  return text;
}

/** 收窄非负有限数值。 */
function requireNonNegativeNumber(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new ClaudeStreamProtocolError(`${label}无效`);
  }
  return value;
}

/** 规范本地构造的非空文本。 */
function requireInputText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}
