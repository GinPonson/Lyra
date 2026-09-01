import { isAbsolute } from "node:path";

import { AgentProtocolError } from "../../domain/agent/index.js";
import { normalizeAgentMcpHttpUrl } from "./mcp-http-config.js";

/** OpenCode 1.18.x 当前实现的 ACP 协议版本。 */
export const OPENCODE_ACP_PROTOCOL_VERSION = 1;

/** ACP JSON-RPC 请求 ID；Lyra 本地构造时统一使用非空字符串。 */
export type OpenCodeAcpRequestId = string | number;

/** OpenCode 会话只接入 Lyra 自身的 HTTP MCP 服务。 */
export interface OpenCodeHttpMcpServer {
  type: "http";
  name: "lyra";
  url: string;
  headers: readonly {
    name: string;
    value: string;
  }[];
}

/** OpenCode ACP 客户端初始化请求。 */
export interface OpenCodeInitializeRequest {
  jsonrpc: "2.0";
  id: string;
  method: "initialize";
  params: {
    protocolVersion: 1;
    clientCapabilities: {
      auth: { terminal: false };
      fs: { readTextFile: false; writeTextFile: false };
      terminal: false;
    };
    clientInfo: {
      name: "Lyra";
      version: string;
    };
  };
}

/** OpenCode ACP 新会话请求；工具仅从显式 Lyra MCP 服务获得。 */
export interface OpenCodeNewSessionRequest {
  jsonrpc: "2.0";
  id: string;
  method: "session/new";
  params: {
    cwd: string;
    mcpServers: readonly OpenCodeHttpMcpServer[];
  };
}

/** OpenCode ACP 文本 prompt 请求。 */
export interface OpenCodePromptRequest {
  jsonrpc: "2.0";
  id: string;
  method: "session/prompt";
  params: {
    sessionId: string;
    prompt: readonly [{ type: "text"; text: string }];
  };
}

/** OpenCode ACP 取消通知；通知不带 JSON-RPC ID。 */
export interface OpenCodeCancelNotification {
  jsonrpc: "2.0";
  method: "session/cancel";
  params: {
    sessionId: string;
  };
}

/** OpenCode 初始化握手的必要公开能力。 */
export interface OpenCodeInitializeResult {
  protocolVersion: 1;
  agentName: string;
  agentVersion: string;
  supportsHttpMcp: boolean;
}

/** OpenCode 创建的供应商会话身份。 */
export interface OpenCodeNewSessionResult {
  sessionId: string;
}

/** OpenCode 回合适配器输出的统一信号和必要协议响应。 */
export type OpenCodeAcpAdapterSignal =
  | { kind: "message_delta"; delta: string }
  | { kind: "reasoning_delta"; delta: string }
  | { kind: "message_snapshot"; content: string; stopReason: string }
  | { kind: "message_failed"; outcome: "failed" | "cancelled" }
  | { kind: "tool_started"; toolCallId: string; title: string; toolKind: string }
  | { kind: "tool_completed"; toolCallId: string; isError: boolean }
  | { kind: "usage_updated"; totalTokens: number; totalCost: number }
  | { kind: "turn_settled" }
  | { kind: "client_response_required"; response: Readonly<Record<string, unknown>> }
  | { kind: "ignored"; sourceType: string };

/** OpenCode ACP 协议异常；JSON-RPC 原始错误只保留在内部诊断字段。 */
export class OpenCodeAcpProtocolError extends AgentProtocolError {
  /** @param internalReason - 当前 OpenCode 版本与 ACP 帧不匹配的内部原因。 */
  constructor(internalReason: string) {
    super("opencode", internalReason);
    this.name = "OpenCodeAcpProtocolError";
  }
}

const TOOL_KINDS = new Set([
  "read",
  "edit",
  "delete",
  "move",
  "search",
  "execute",
  "think",
  "fetch",
  "switch_mode",
  "other",
]);
const IGNORED_SESSION_UPDATES = new Set([
  "user_message_chunk",
  "plan",
  "available_commands_update",
  "current_mode_update",
  "config_option_update",
  "session_info_update",
]);

/** 构造不授予文件、终端或终端认证能力的 ACP 初始化请求。 */
export function buildOpenCodeInitializeRequest(
  requestId: string,
  clientVersion: string,
): OpenCodeInitializeRequest {
  return {
    jsonrpc: "2.0",
    id: requireInputText(requestId, "OpenCode initialize request ID"),
    method: "initialize",
    params: {
      protocolVersion: OPENCODE_ACP_PROTOCOL_VERSION,
      clientCapabilities: {
        auth: { terminal: false },
        fs: { readTextFile: false, writeTextFile: false },
        terminal: false,
      },
      clientInfo: {
        name: "Lyra",
        version: requireInputText(clientVersion, "Lyra 客户端版本"),
      },
    },
  };
}

/**
 * 构造绑定隔离工作目录和唯一 Lyra MCP 的新会话请求。
 *
 * MCP header 可携带服务端签发的短期会话凭据；该帧不得写入日志或数据库。
 */
export function buildOpenCodeNewSessionRequest(input: {
  requestId: string;
  workDirectory: string;
  mcpServer: OpenCodeHttpMcpServer;
}): OpenCodeNewSessionRequest {
  if (!isAbsolute(input.workDirectory)) {
    throw new RangeError("OpenCode ACP 工作目录必须是绝对路径");
  }
  return {
    jsonrpc: "2.0",
    id: requireInputText(input.requestId, "OpenCode new session request ID"),
    method: "session/new",
    params: {
      cwd: input.workDirectory,
      mcpServers: [normalizeMcpServer(input.mcpServer)],
    },
  };
}

/** 构造只含一个文本 content block 的 OpenCode prompt 请求。 */
export function buildOpenCodePromptRequest(input: {
  requestId: string;
  sessionId: string;
  content: string;
}): OpenCodePromptRequest {
  return {
    jsonrpc: "2.0",
    id: requireInputText(input.requestId, "OpenCode prompt request ID"),
    method: "session/prompt",
    params: {
      sessionId: requireInputText(input.sessionId, "OpenCode session ID"),
      prompt: [{ type: "text", text: requireInputText(input.content, "OpenCode prompt") }],
    },
  };
}

/** 构造 ACP 取消通知；实际会话状态仍由 Lyra lifecycle 先转为 cancelling。 */
export function buildOpenCodeCancelNotification(sessionId: string): OpenCodeCancelNotification {
  return {
    jsonrpc: "2.0",
    method: "session/cancel",
    params: { sessionId: requireInputText(sessionId, "OpenCode session ID") },
  };
}

/** 验证 initialize 响应及 OpenCode 必需的 HTTP MCP 能力。 */
export function parseOpenCodeInitializeResponse(
  frame: unknown,
  requestId: OpenCodeAcpRequestId,
): OpenCodeInitializeResult {
  const result = requireResponseResult(frame, requestId, "OpenCode initialize");
  if (result.protocolVersion !== OPENCODE_ACP_PROTOCOL_VERSION) {
    throw new OpenCodeAcpProtocolError("OpenCode ACP 协议版本不受支持");
  }
  const agentInfo = requireRecord(result.agentInfo, "OpenCode agentInfo");
  const capabilities = requireRecord(result.agentCapabilities, "OpenCode agentCapabilities");
  const mcpCapabilities = requireRecord(
    capabilities.mcpCapabilities,
    "OpenCode MCP capabilities",
  );
  if (mcpCapabilities.http !== true) {
    throw new OpenCodeAcpProtocolError("OpenCode 不支持 Lyra 必需的 HTTP MCP");
  }
  return {
    protocolVersion: OPENCODE_ACP_PROTOCOL_VERSION,
    agentName: requireTextValue(agentInfo.name, "OpenCode agent name"),
    agentVersion: requireTextValue(agentInfo.version, "OpenCode agent version"),
    supportsHttpMcp: true,
  };
}

/** 验证 session/new 响应并取得 OpenCode 供应商会话 ID。 */
export function parseOpenCodeNewSessionResponse(
  frame: unknown,
  requestId: OpenCodeAcpRequestId,
): OpenCodeNewSessionResult {
  const result = requireResponseResult(frame, requestId, "OpenCode new session");
  return { sessionId: requireTextValue(result.sessionId, "OpenCode session ID") };
}

/**
 * 聚合 OpenCode 单个 session/prompt 回合。
 *
 * ACP 最终响应只包含 stopReason，不重复完整文本，因此适配器在内存中累计
 * agent_message_chunk，并在 prompt 响应到达时生成权威快照。reasoning 不进入
 * 累计文本；usage_update 只保存美元成本，token 以 prompt result 为准。
 */
export class OpenCodeAcpTurnAdapter {
  private readonly sessionId: string;
  private readonly promptRequestId: OpenCodeAcpRequestId;
  private visibleContent = "";
  private totalCost = 0;
  private settled = false;

  /** @param input - 已创建的供应商会话 ID 与当前 prompt JSON-RPC ID。 */
  constructor(input: { sessionId: string; promptRequestId: OpenCodeAcpRequestId }) {
    this.sessionId = requireInputText(input.sessionId, "OpenCode session ID");
    requireRequestId(input.promptRequestId, "OpenCode prompt request ID");
    this.promptRequestId = input.promptRequestId;
  }

  /** 解析一个 ACP JSON Lines 帧，并返回零个或多个统一信号。 */
  parseFrame(frame: unknown): readonly OpenCodeAcpAdapterSignal[] {
    if (this.settled) {
      throw new OpenCodeAcpProtocolError("OpenCode 回合 settled 后收到迟到帧");
    }
    const record = requireJsonRpcFrame(frame);
    if (record.method !== undefined) {
      const method = requireTextValue(record.method, "OpenCode ACP method");
      return record.id === undefined
        ? this.parseNotification(method, record)
        : this.parseClientRequest(method, record);
    }
    return this.parsePromptResponse(record);
  }

  /** 解析 OpenCode 主动发给客户端的通知。 */
  private parseNotification(
    method: string,
    frame: Readonly<Record<string, unknown>>,
  ): readonly OpenCodeAcpAdapterSignal[] {
    if (method !== "session/update") {
      throw new OpenCodeAcpProtocolError(`未知 OpenCode ACP notification: ${method}`);
    }
    const params = requireRecord(frame.params, "OpenCode session/update params");
    requireMatchingSession(params.sessionId, this.sessionId);
    const update = requireRecord(params.update, "OpenCode session update");
    const updateType = requireTextValue(update.sessionUpdate, "OpenCode session update 类型");
    switch (updateType) {
      case "agent_message_chunk": {
        const text = parseTextContent(update.content, "OpenCode assistant chunk");
        if (text.length === 0) {
          return [];
        }
        this.visibleContent += text;
        return [{ kind: "message_delta", delta: text }];
      }
      case "agent_thought_chunk": {
        const text = parseTextContent(update.content, "OpenCode thought chunk");
        return text.length === 0 ? [] : [{ kind: "reasoning_delta", delta: text }];
      }
      case "tool_call":
        return [parseToolStart(update)];
      case "tool_call_update":
        return parseToolUpdate(update);
      case "usage_update":
        this.totalCost = parseUsageUpdateCost(update);
        return [{ kind: "ignored", sourceType: "session/update:usage_update" }];
      default:
        if (IGNORED_SESSION_UPDATES.has(updateType)) {
          return [{ kind: "ignored", sourceType: `session/update:${updateType}` }];
        }
        throw new OpenCodeAcpProtocolError(`未知 OpenCode session update: ${updateType}`);
    }
  }

  /**
   * 拒绝 Agent 发起的客户端能力请求。
   *
   * 权限请求使用 ACP cancelled outcome；文件、终端和 elicitation 等其他方法
   * 使用标准 method-not-found 错误，防止请求悬挂又不授予宿主能力。
   */
  private parseClientRequest(
    method: string,
    frame: Readonly<Record<string, unknown>>,
  ): readonly OpenCodeAcpAdapterSignal[] {
    const requestId = requireRequestId(frame.id, "OpenCode client request ID");
    const params = requireRecord(frame.params, "OpenCode client request params");
    if (params.sessionId !== undefined) {
      requireMatchingSession(params.sessionId, this.sessionId);
    }
    if (method === "session/request_permission") {
      return [{
        kind: "client_response_required",
        response: {
          jsonrpc: "2.0",
          id: requestId,
          result: { outcome: { outcome: "cancelled" } },
        },
      }];
    }
    return [{
      kind: "client_response_required",
      response: {
        jsonrpc: "2.0",
        id: requestId,
        error: {
          code: -32601,
          message: "Lyra ACP client capability disabled",
        },
      },
    }];
  }

  /** 把 prompt response 转换为最终快照、usage、失败分类和 settled。 */
  private parsePromptResponse(
    frame: Readonly<Record<string, unknown>>,
  ): readonly OpenCodeAcpAdapterSignal[] {
    const responseId = requireRequestId(frame.id, "OpenCode response ID");
    if (responseId !== this.promptRequestId) {
      throw new OpenCodeAcpProtocolError("OpenCode response ID 与当前 prompt 不匹配");
    }
    if (frame.error !== undefined) {
      requireRecord(frame.error, "OpenCode JSON-RPC error");
      this.settled = true;
      return [{ kind: "message_failed", outcome: "failed" }, { kind: "turn_settled" }];
    }
    const result = requireRecord(frame.result, "OpenCode prompt result");
    const stopReason = requireTextValue(result.stopReason, "OpenCode stop reason");
    const usageSignal = result.usage === null || result.usage === undefined
      ? []
      : [parsePromptUsage(result.usage, this.totalCost)];
    this.settled = true;
    if (stopReason === "end_turn") {
      return [
        { kind: "message_snapshot", content: this.visibleContent, stopReason },
        ...usageSignal,
        { kind: "turn_settled" },
      ];
    }
    if (stopReason === "cancelled") {
      return [
        ...usageSignal,
        { kind: "message_failed", outcome: "cancelled" },
        { kind: "turn_settled" },
      ];
    }
    if (["max_tokens", "max_turn_requests", "refusal"].includes(stopReason)) {
      return [
        ...usageSignal,
        { kind: "message_failed", outcome: "failed" },
        { kind: "turn_settled" },
      ];
    }
    throw new OpenCodeAcpProtocolError(`未知 OpenCode stop reason: ${stopReason}`);
  }
}

/** 解析 tool_call 起始通知，不读取 rawInput 或文件位置。 */
function parseToolStart(
  update: Readonly<Record<string, unknown>>,
): Extract<OpenCodeAcpAdapterSignal, { kind: "tool_started" }> {
  const toolKind = update.kind === undefined
    ? "other"
    : requireTextValue(update.kind, "OpenCode tool kind");
  if (!TOOL_KINDS.has(toolKind)) {
    throw new OpenCodeAcpProtocolError(`未知 OpenCode tool kind: ${toolKind}`);
  }
  return {
    kind: "tool_started",
    toolCallId: requireTextValue(update.toolCallId, "OpenCode tool call ID"),
    title: requireTextValue(update.title, "OpenCode tool title"),
    toolKind,
  };
}

/** 仅在 tool_call_update 到达终态时生成完成信号。 */
function parseToolUpdate(
  update: Readonly<Record<string, unknown>>,
): readonly OpenCodeAcpAdapterSignal[] {
  const toolCallId = requireTextValue(update.toolCallId, "OpenCode tool call ID");
  if (update.status === null || update.status === undefined) {
    return [{ kind: "ignored", sourceType: "session/update:tool_call_update" }];
  }
  const status = requireTextValue(update.status, "OpenCode tool status");
  switch (status) {
    case "pending":
    case "in_progress":
      return [{ kind: "ignored", sourceType: `tool_call_update:${status}` }];
    case "completed":
      return [{ kind: "tool_completed", toolCallId, isError: false }];
    case "failed":
      return [{ kind: "tool_completed", toolCallId, isError: true }];
    default:
      throw new OpenCodeAcpProtocolError(`未知 OpenCode tool status: ${status}`);
  }
}

/** usage_update 的 used 是上下文占用而非回合 token，只取累计美元成本。 */
function parseUsageUpdateCost(update: Readonly<Record<string, unknown>>): number {
  requireNonNegativeNumber(update.used, "OpenCode used context tokens");
  requirePositiveNumber(update.size, "OpenCode context size");
  if (update.cost === null || update.cost === undefined) {
    return 0;
  }
  const cost = requireRecord(update.cost, "OpenCode usage cost");
  if (cost.currency !== "USD") {
    throw new OpenCodeAcpProtocolError("OpenCode usage cost 不是 USD");
  }
  return requireNonNegativeNumber(cost.amount, "OpenCode usage cost amount");
}

/** 验证 prompt usage 的总数与各分项一致。 */
function parsePromptUsage(
  value: unknown,
  totalCost: number,
): Extract<OpenCodeAcpAdapterSignal, { kind: "usage_updated" }> {
  const usage = requireRecord(value, "OpenCode prompt usage");
  const inputTokens = requireNonNegativeNumber(usage.inputTokens, "OpenCode input tokens");
  const outputTokens = requireNonNegativeNumber(usage.outputTokens, "OpenCode output tokens");
  const thoughtTokens = optionalNonNegativeNumber(usage.thoughtTokens, "OpenCode thought tokens");
  const cachedReadTokens = optionalNonNegativeNumber(
    usage.cachedReadTokens,
    "OpenCode cached read tokens",
  );
  const cachedWriteTokens = optionalNonNegativeNumber(
    usage.cachedWriteTokens,
    "OpenCode cached write tokens",
  );
  const totalTokens = requireNonNegativeNumber(usage.totalTokens, "OpenCode total tokens");
  if (totalTokens !== (
    inputTokens + outputTokens + thoughtTokens + cachedReadTokens + cachedWriteTokens
  )) {
    throw new OpenCodeAcpProtocolError("OpenCode total tokens 与 usage 分项不一致");
  }
  return { kind: "usage_updated", totalTokens, totalCost };
}

/** ACP message/thought chunk 在当前 OpenCode 实现中必须是 text content。 */
function parseTextContent(value: unknown, label: string): string {
  const content = requireRecord(value, label);
  if (content.type !== "text") {
    throw new OpenCodeAcpProtocolError(`${label}不是文本内容`);
  }
  return requireString(content.text, `${label} text`);
}

/** 验证 JSON-RPC 版本并收窄对象。 */
function requireJsonRpcFrame(value: unknown): Readonly<Record<string, unknown>> {
  const frame = requireRecord(value, "OpenCode ACP 帧");
  if (frame.jsonrpc !== "2.0") {
    throw new OpenCodeAcpProtocolError("OpenCode ACP 帧缺少 JSON-RPC 2.0 标识");
  }
  return frame;
}

/** 验证握手或建会话的关联成功响应。 */
function requireResponseResult(
  frame: unknown,
  requestId: OpenCodeAcpRequestId,
  label: string,
): Readonly<Record<string, unknown>> {
  requireRequestId(requestId, `${label} request ID`);
  const record = requireJsonRpcFrame(frame);
  const responseId = requireRequestId(record.id, `${label} response ID`);
  if (responseId !== requestId) {
    throw new OpenCodeAcpProtocolError(`${label} response ID 不匹配`);
  }
  if (record.error !== undefined) {
    const error = requireRecord(record.error, `${label} error`);
    const internalMessage = typeof error.message === "string" ? error.message : "missing error";
    throw new OpenCodeAcpProtocolError(`${label} 被拒绝: ${internalMessage}`);
  }
  return requireRecord(record.result, `${label} result`);
}

/** 复制并验证唯一 Lyra HTTP MCP 描述。 */
function normalizeMcpServer(input: OpenCodeHttpMcpServer): OpenCodeHttpMcpServer {
  if (input.type !== "http" || input.name !== "lyra") {
    throw new RangeError("OpenCode ACP 只允许名为 lyra 的 HTTP MCP");
  }
  const headers = input.headers.map((header) => {
    if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(header.name)) {
      throw new RangeError("OpenCode Lyra MCP header 名无效");
    }
    if (header.value.length === 0 || /[\r\n]/.test(header.value)) {
      throw new RangeError("OpenCode Lyra MCP header 值无效");
    }
    return { name: header.name, value: header.value };
  });
  return {
    type: "http",
    name: "lyra",
    url: normalizeAgentMcpHttpUrl(input.url),
    headers,
  };
}

/** 收窄 JSON object，数组不视为对象。 */
function requireRecord(value: unknown, label: string): Readonly<Record<string, unknown>> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new OpenCodeAcpProtocolError(`${label}不是对象`);
  }
  return value as Readonly<Record<string, unknown>>;
}

/** 接受可为空协议字符串。 */
function requireString(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new OpenCodeAcpProtocolError(`${label}不是字符串`);
  }
  return value;
}

/** 接受非空协议字符串。 */
function requireTextValue(value: unknown, label: string): string {
  const text = requireString(value, label);
  if (text.length === 0) {
    throw new OpenCodeAcpProtocolError(`${label}为空`);
  }
  return text;
}

/** 验证 JSON-RPC 字符串或数值 ID。 */
function requireRequestId(value: unknown, label: string): OpenCodeAcpRequestId {
  if (typeof value === "string" && value.length > 0) {
    return value;
  }
  if (typeof value === "number" && Number.isSafeInteger(value)) {
    return value;
  }
  throw new OpenCodeAcpProtocolError(`${label}无效`);
}

/** 验证通知归属当前供应商会话。 */
function requireMatchingSession(value: unknown, expected: string): void {
  if (requireTextValue(value, "OpenCode notification session ID") !== expected) {
    throw new OpenCodeAcpProtocolError("OpenCode notification 跨会话");
  }
}

/** 收窄非负有限数值。 */
function requireNonNegativeNumber(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new OpenCodeAcpProtocolError(`${label}无效`);
  }
  return value;
}

/** 收窄正有限数值。 */
function requirePositiveNumber(value: unknown, label: string): number {
  const number = requireNonNegativeNumber(value, label);
  if (number === 0) {
    throw new OpenCodeAcpProtocolError(`${label}必须大于零`);
  }
  return number;
}

/** ACP nullable token 分项缺省时按协议语义计为零。 */
function optionalNonNegativeNumber(value: unknown, label: string): number {
  return value === null || value === undefined ? 0 : requireNonNegativeNumber(value, label);
}

/** 规范 Lyra 本地构造的非空文本。 */
function requireInputText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}
