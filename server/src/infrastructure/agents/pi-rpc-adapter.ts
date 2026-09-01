/** Pi RPC prompt 命令；不支持图片、steer 或 follow-up 绕过 Lyra 回合边界。 */
export interface PiPromptCommand {
  id: string;
  type: "prompt";
  message: string;
}

/** Pi RPC 取消命令。 */
export interface PiAbortCommand {
  id: string;
  type: "abort";
}

/** Pi RPC 帧转换后的供应商无关信号。 */
export type PiRpcAdapterSignal =
  | { kind: "command_accepted"; command: "prompt" | "abort"; requestId: string | null }
  | { kind: "message_delta"; delta: string }
  | { kind: "reasoning_delta"; delta: string }
  | { kind: "message_snapshot"; content: string; stopReason: string }
  | { kind: "message_failed"; outcome: "failed" | "cancelled" }
  | {
      kind: "tool_requested";
      toolCallId: string;
      toolName: string;
      arguments: Readonly<Record<string, unknown>>;
    }
  | { kind: "tool_started"; toolCallId: string; toolName: string }
  | { kind: "tool_completed"; toolCallId: string; toolName: string; isError: boolean }
  | { kind: "usage_updated"; totalTokens: number; totalCost: number }
  | { kind: "turn_settled" }
  | { kind: "ignored"; sourceType: string };

/** Pi RPC 协议异常；原始供应商错误仅留在服务端，不进入客户端消息。 */
export class PiRpcProtocolError extends AgentProtocolError {
  /** @param internalReason - 仅供服务端日志定位协议版本或供应商错误。 */
  constructor(internalReason: string) {
    super("pi", internalReason);
    this.name = "PiRpcProtocolError";
  }
}

/** Pi 会产生但第一阶段无需转发或持久化的已知生命周期事件。 */
const KNOWN_IGNORED_EVENT_TYPES = new Set([
  "agent_start",
  "agent_end",
  "turn_start",
  "turn_end",
  "message_start",
  "queue_update",
  "compaction_start",
  "compaction_end",
  "entry_appended",
  "session_info_changed",
  "thinking_level_changed",
  "auto_retry_start",
  "auto_retry_end",
  "summarization_retry_scheduled",
  "summarization_retry_attempt_start",
  "summarization_retry_finished",
]);

/** 构造关联当前逻辑回合的 Pi prompt 帧。 */
export function buildPiPromptCommand(requestId: string, content: string): PiPromptCommand {
  return {
    id: requireText(requestId, "Pi prompt 请求 ID"),
    type: "prompt",
    message: requireText(content, "Pi prompt 内容"),
  };
}

/** 构造可与 prompt 响应独立关联的 Pi abort 帧。 */
export function buildPiAbortCommand(requestId: string): PiAbortCommand {
  return {
    id: requireText(requestId, "Pi abort 请求 ID"),
    type: "abort",
  };
}

/**
 * 把本机 Pi 0.84.x RPC 帧转换为 Lyra 编排器信号。
 *
 * `thinking_delta` 只用于实时状态，不会进入最终消息快照。工具参数只在
 * `tool_requested` 信号中交给白名单策略层，持久化仍必须使用安全摘要计划。
 * 响应中的 error、extension UI 和未知帧均明确失败，不静默降级。
 *
 * @param frame - 受控 JSON Lines 运行时解析出的单个未知 JSON 值。
 * @returns 零个或多个规范信号；空数组只用于没有新增信息的 delta 边界帧。
 */
export function parsePiRpcFrame(frame: unknown): readonly PiRpcAdapterSignal[] {
  const record = requireRecord(frame, "Pi RPC 帧");
  const type = requireTextValue(record.type, "Pi RPC 帧类型");
  switch (type) {
    case "response":
      return parseResponse(record);
    case "message_update":
      return parseMessageUpdate(record);
    case "message_end":
      return parseMessageEnd(record);
    case "tool_execution_start":
      return [{
        kind: "tool_started",
        toolCallId: requireTextValue(record.toolCallId, "Pi tool call ID"),
        toolName: requireToolName(record.toolName),
      }];
    case "tool_execution_update":
      return [{ kind: "ignored", sourceType: type }];
    case "tool_execution_end":
      return [{
        kind: "tool_completed",
        toolCallId: requireTextValue(record.toolCallId, "Pi tool call ID"),
        toolName: requireToolName(record.toolName),
        isError: requireBoolean(record.isError, "Pi 工具错误状态"),
      }];
    case "agent_settled":
      return [{ kind: "turn_settled" }];
    case "extension_ui_request":
      throw new PiRpcProtocolError("Pi RPC 请求了第一阶段禁止的交互式扩展 UI");
    case "extension_error":
      throw new PiRpcProtocolError("Pi 扩展执行失败");
    default:
      if (KNOWN_IGNORED_EVENT_TYPES.has(type)) {
        return [{ kind: "ignored", sourceType: type }];
      }
      throw new PiRpcProtocolError(`未知 Pi RPC 帧类型: ${type}`);
  }
}

/** 解析 prompt/abort 的关联响应，不向客户端复制供应商 error 文本。 */
function parseResponse(record: Readonly<Record<string, unknown>>): readonly PiRpcAdapterSignal[] {
  const command = requireTextValue(record.command, "Pi RPC 响应命令");
  if (command !== "prompt" && command !== "abort") {
    return [{ kind: "ignored", sourceType: `response:${command}` }];
  }
  if (record.success !== true) {
    const internalError = typeof record.error === "string" ? record.error : "missing error";
    throw new PiRpcProtocolError(`Pi ${command} 被拒绝: ${internalError}`);
  }
  return [{
    kind: "command_accepted",
    command,
    requestId: typeof record.id === "string" && record.id.length > 0 ? record.id : null,
  }];
}

/** 解析 Pi 去累计快照后的 AssistantMessageEvent。 */
function parseMessageUpdate(
  record: Readonly<Record<string, unknown>>,
): readonly PiRpcAdapterSignal[] {
  const event = requireRecord(record.assistantMessageEvent, "Pi assistant message event");
  const eventType = requireTextValue(event.type, "Pi assistant message event 类型");
  switch (eventType) {
    case "text_delta": {
      const delta = requireString(event.delta, "Pi 文本增量");
      return delta.length === 0 ? [] : [{ kind: "message_delta", delta }];
    }
    case "thinking_delta": {
      const delta = requireString(event.delta, "Pi reasoning 增量");
      return delta.length === 0 ? [] : [{ kind: "reasoning_delta", delta }];
    }
    case "toolcall_end": {
      const toolCall = requireRecord(event.toolCall, "Pi tool call");
      return [{
        kind: "tool_requested",
        toolCallId: requireTextValue(toolCall.id, "Pi tool call ID"),
        toolName: requireToolName(toolCall.name),
        arguments: requireRecord(toolCall.arguments, "Pi 工具参数"),
      }];
    }
    case "error": {
      const reason = requireTextValue(event.reason, "Pi 助手失败原因");
      return [{
        kind: "message_failed",
        outcome: reason === "aborted" ? "cancelled" : "failed",
      }];
    }
    case "start":
    case "text_start":
    case "text_end":
    case "thinking_start":
    case "thinking_end":
    case "toolcall_start":
    case "toolcall_delta":
    case "done":
      return parseUsage(record.usage);
    default:
      throw new PiRpcProtocolError(`未知 Pi assistant message event: ${eventType}`);
  }
}

/** 只提取 message_end 中用户可见的 text block，并忽略 thinking/toolCall 正文。 */
function parseMessageEnd(record: Readonly<Record<string, unknown>>): readonly PiRpcAdapterSignal[] {
  const message = requireRecord(record.message, "Pi 最终消息");
  if (message.role !== "assistant") {
    return [{ kind: "ignored", sourceType: "message_end:non_assistant" }];
  }
  const stopReason = requireTextValue(message.stopReason, "Pi stop reason");
  if (stopReason === "error" || stopReason === "aborted") {
    return [{
      kind: "message_failed",
      outcome: stopReason === "aborted" ? "cancelled" : "failed",
    }];
  }
  if (!Array.isArray(message.content)) {
    throw new PiRpcProtocolError("Pi 助手消息 content 不是数组");
  }
  const textParts: string[] = [];
  for (const part of message.content) {
    const contentPart = requireRecord(part, "Pi 助手内容块");
    if (contentPart.type === "text") {
      textParts.push(requireString(contentPart.text, "Pi 助手文本"));
    }
  }
  return [
    {
      kind: "message_snapshot",
      content: textParts.join(""),
      stopReason,
    },
    ...parseUsage(message.usage),
  ];
}

/** 将供应商 usage 缩减为 UI 所需的总 token 和总成本。 */
function parseUsage(value: unknown): readonly PiRpcAdapterSignal[] {
  if (value === undefined) {
    return [];
  }
  const usage = requireRecord(value, "Pi usage");
  const cost = requireRecord(usage.cost, "Pi usage cost");
  const totalTokens = requireNonNegativeNumber(usage.totalTokens, "Pi total tokens");
  const totalCost = requireNonNegativeNumber(cost.total, "Pi total cost");
  return [{ kind: "usage_updated", totalTokens, totalCost }];
}

/** 收窄 JSON object，数组不视为对象。 */
function requireRecord(value: unknown, label: string): Readonly<Record<string, unknown>> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new PiRpcProtocolError(`${label}不是对象`);
  }
  return value as Readonly<Record<string, unknown>>;
}

/** 接受可为空的增量字符串。 */
function requireString(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new PiRpcProtocolError(`${label}不是字符串`);
  }
  return value;
}

/** 接受非空协议文本。 */
function requireTextValue(value: unknown, label: string): string {
  const text = requireString(value, label);
  if (text.length === 0) {
    throw new PiRpcProtocolError(`${label}为空`);
  }
  return text;
}

/** 工具名必须适合策略匹配和最小审计。 */
function requireToolName(value: unknown): string {
  const name = requireTextValue(value, "Pi 工具名");
  if (!/^[a-z][a-z0-9-]{0,63}$/.test(name)) {
    throw new PiRpcProtocolError("Pi 工具名格式无效");
  }
  return name;
}

/** 收窄布尔协议字段。 */
function requireBoolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new PiRpcProtocolError(`${label}不是布尔值`);
  }
  return value;
}

/** 收窄非负有限数值。 */
function requireNonNegativeNumber(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new PiRpcProtocolError(`${label}无效`);
  }
  return value;
}

/** 规范本地发出的命令文本。 */
function requireText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}
import { AgentProtocolError } from "../../domain/agent/index.js";
