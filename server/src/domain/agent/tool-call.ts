import { AgentEventSequencer } from "./events.js";
import type { AgentEvent } from "./events.js";
import { AgentSessionStateError } from "./lifecycle.js";
import {
  FINANCIAL_TOOL_NAMES,
  AgentToolPolicyError,
} from "./tool-registry.js";
import type { FinancialToolName } from "./tool-registry.js";
import type { AgentSessionSnapshot } from "./switching.js";
import { createAuditEvent } from "../audit/index.js";
import type { AuditEvent } from "../audit/index.js";

/** 可持久化的工具参数或结果摘要；禁止嵌套原始查询结果。 */
export type AgentToolSummary = Readonly<Record<string, string | number | boolean | null>>;

/** 工具调用的非成功终态。 */
export type AgentToolFailureOutcome = "failed" | "cancelled";

/** 未执行工具请求的稳定策略分类，不包含供应商原始错误。 */
export type AgentToolRejectionReason =
  | "unknown_tool"
  | "unconfigured_tool"
  | "forbidden_capability";

/** 与 DuckDB `agent_tool_calls` 对齐的规范工具调用记录。 */
export interface AgentToolCallRecord {
  id: string;
  sessionId: string;
  messageId: string;
  ordinal: number;
  toolName: string;
  authorizationDecision: "allowed" | "denied";
  status: "started" | "completed" | AgentToolFailureOutcome | "rejected";
  argumentsSummary: AgentToolSummary;
  resultSummary: AgentToolSummary | null;
  ledgerVersion: number;
  durationMs: number | null;
  errorCode:
    | "AGENT_TOOL_EXECUTION_FAILED"
    | "AGENT_TOOL_CANCELLED"
    | "AGENT_TOOL_FORBIDDEN"
    | null;
  startedAt: string;
  completedAt: string | null;
}

/** 工具开始时需要原子固化的记录和实时事件。 */
export interface AgentToolCallStartPlan {
  call: AgentToolCallRecord;
  event: AgentEvent;
}

/** 工具到达持久终态时需要原子固化的记录和实时事件。 */
export interface AgentToolCallCompletionPlan {
  call: AgentToolCallRecord;
  event: AgentEvent;
}

/** 策略拒绝需要一起固化的调用记录、客户端事件和最小审计事件。 */
export interface AgentToolCallRejectionPlan extends AgentToolCallCompletionPlan {
  auditEvent: AuditEvent;
}

/** 明确禁止进入参数或结果摘要的敏感字段。 */
const FORBIDDEN_SUMMARY_KEYS = new Set([
  "authorization",
  "cookie",
  "oauthcode",
  "accesstoken",
  "refreshtoken",
  "token",
  "email",
  "accountnumber",
  "mailbody",
  "body",
  "prompt",
  "hiddenreasoning",
  "privatekey",
]);

/**
 * 为已经通过白名单授权的只读金融工具生成开始计划。
 *
 * @param input - running 会话、已持久化消息、调用身份、顺序和安全参数摘要。
 * @returns 绑定服务端账本版本的 started 记录及无原始参数事件。
 */
export function startAgentToolCall(input: {
  session: AgentSessionSnapshot;
  messageId: string;
  callId: string;
  ordinal: number;
  toolName: string;
  argumentsSummary: AgentToolSummary;
  startedAt: string;
  lastEventSequence: number;
}): AgentToolCallStartPlan {
  if (input.session.status !== "running") {
    throw new AgentSessionStateError(input.session.status, "running");
  }
  const messageId = requirePersistedMessage(input.session, input.messageId);
  const callId = requireText(input.callId, "Agent 工具调用 ID");
  if (!Number.isSafeInteger(input.ordinal) || input.ordinal <= 0) {
    throw new RangeError("Agent 工具调用顺序必须是正安全整数");
  }
  if (!(FINANCIAL_TOOL_NAMES as readonly string[]).includes(input.toolName)) {
    throw new AgentToolPolicyError(`Agent 工具不在只读白名单中: ${input.toolName}`);
  }
  const toolName = input.toolName as FinancialToolName;
  const startedAt = requireText(input.startedAt, "Agent 工具开始时间");
  const call: AgentToolCallRecord = {
    id: callId,
    sessionId: input.session.id,
    messageId,
    ordinal: input.ordinal,
    toolName,
    authorizationDecision: "allowed",
    status: "started",
    argumentsSummary: normalizeSummary(input.argumentsSummary, "参数"),
    resultSummary: null,
    ledgerVersion: input.session.ledgerVersion,
    durationMs: null,
    errorCode: null,
    startedAt,
    completedAt: null,
  };
  const event = new AgentEventSequencer(
    input.session.id,
    input.lastEventSequence,
  ).next({
    type: "tool.started",
    messageId,
    occurredAt: startedAt,
    payload: {
      tool_call_id: callId,
      tool_name: toolName,
      ledger_version: input.session.ledgerVersion,
    },
  });
  return { call, event };
}

/**
 * 把 started 工具调用固化为成功、失败或取消终态。
 *
 * 成功只保存调用方生成的有界结果摘要；失败与取消不保存部分结果或底层异常，
 * 仅写稳定错误码。统一 `tool.completed` 事件通过 outcome 区分终态。
 *
 * @param input - started 记录、终态、安全结果摘要、耗时、时间与事件序号。
 * @returns 可持久化终态记录及不含原始工具结果的完成事件。
 */
export function completeAgentToolCall(input: {
  call: AgentToolCallRecord;
  outcome: "completed" | AgentToolFailureOutcome;
  resultSummary: AgentToolSummary | null;
  durationMs: number;
  completedAt: string;
  lastEventSequence: number;
}): AgentToolCallCompletionPlan {
  if (input.call.status !== "started") {
    throw new RangeError("只有 started 工具调用可以进入终态");
  }
  if (!Number.isSafeInteger(input.durationMs) || input.durationMs < 0) {
    throw new RangeError("Agent 工具耗时必须是非负安全整数");
  }
  const completedAt = requireText(input.completedAt, "Agent 工具完成时间");
  if (input.outcome === "completed" && input.resultSummary === null) {
    throw new RangeError("成功工具调用必须包含结果摘要");
  }
  if (input.outcome !== "completed" && input.resultSummary !== null) {
    throw new RangeError("失败或取消工具调用不能保存部分结果摘要");
  }
  const errorCode = input.outcome === "failed"
    ? "AGENT_TOOL_EXECUTION_FAILED"
    : input.outcome === "cancelled"
      ? "AGENT_TOOL_CANCELLED"
      : null;
  const call: AgentToolCallRecord = {
    ...input.call,
    status: input.outcome,
    resultSummary: input.resultSummary === null
      ? null
      : normalizeSummary(input.resultSummary, "结果"),
    durationMs: input.durationMs,
    errorCode,
    completedAt,
  };
  const event = new AgentEventSequencer(
    input.call.sessionId,
    input.lastEventSequence,
  ).next({
    type: "tool.completed",
    messageId: input.call.messageId,
    occurredAt: completedAt,
    payload: {
      tool_call_id: input.call.id,
      tool_name: input.call.toolName,
      ledger_version: input.call.ledgerVersion,
      outcome: input.outcome,
      duration_ms: input.durationMs,
      error_code: errorCode,
    },
  });
  return { call, event };
}

/**
 * 记录一个没有执行的越权、未知或未配置工具请求。
 *
 * 请求参数不会进入记录、事件或审计元数据；调用方只提供规范工具名和稳定策略
 * 分类。拒绝直接形成 `rejected` 终态，不产生误导性的 started 事件。
 *
 * @param input - running 会话、已持久化消息、调用身份、策略分类和关联请求。
 * @returns denied/rejected 调用、统一完成事件和最小审计事件。
 */
export function rejectAgentToolCall(input: {
  session: AgentSessionSnapshot;
  messageId: string;
  callId: string;
  ordinal: number;
  requestedToolName: string;
  reason: AgentToolRejectionReason;
  rejectedAt: string;
  lastEventSequence: number;
  requestId?: string | null;
}): AgentToolCallRejectionPlan {
  if (input.session.status !== "running") {
    throw new AgentSessionStateError(input.session.status, "running");
  }
  const messageId = requirePersistedMessage(input.session, input.messageId);
  const callId = requireText(input.callId, "Agent 工具调用 ID");
  if (!Number.isSafeInteger(input.ordinal) || input.ordinal <= 0) {
    throw new RangeError("Agent 工具调用顺序必须是正安全整数");
  }
  const requestedToolName = requireText(input.requestedToolName, "Agent 请求工具名");
  if (!/^[a-z][a-z0-9-]{0,63}$/.test(requestedToolName)) {
    throw new RangeError("Agent 请求工具名格式无效");
  }
  const rejectedAt = normalizeTimestamp(input.rejectedAt, "Agent 工具拒绝时间");
  const call: AgentToolCallRecord = {
    id: callId,
    sessionId: input.session.id,
    messageId,
    ordinal: input.ordinal,
    toolName: requestedToolName,
    authorizationDecision: "denied",
    status: "rejected",
    argumentsSummary: {},
    resultSummary: null,
    ledgerVersion: input.session.ledgerVersion,
    durationMs: 0,
    errorCode: "AGENT_TOOL_FORBIDDEN",
    startedAt: rejectedAt,
    completedAt: rejectedAt,
  };
  const event = new AgentEventSequencer(
    input.session.id,
    input.lastEventSequence,
  ).next({
    type: "tool.completed",
    messageId,
    occurredAt: rejectedAt,
    payload: {
      tool_call_id: callId,
      tool_name: requestedToolName,
      ledger_version: input.session.ledgerVersion,
      outcome: "rejected",
      error_code: "AGENT_TOOL_FORBIDDEN",
    },
  });
  const auditEvent = createAuditEvent({
    eventType: "agent.tool_rejected",
    outcome: "denied",
    actorType: "system",
    requestId: input.requestId ?? null,
    targetType: "agent_tool_call",
    targetId: callId,
    accountId: input.session.accountScope.kind === "account"
      ? input.session.accountScope.account
      : null,
    ledgerVersion: input.session.ledgerVersion,
    errorCode: "AGENT_TOOL_FORBIDDEN",
    metadata: {
      tool_name: requestedToolName,
      policy_reason: input.reason,
      provider: input.session.provider,
    },
    now: new Date(rejectedAt),
  });
  return { call, event, auditEvent };
}

/** 复制并验证扁平、有界摘要，避免把原始响应或敏感字段误写入数据库。 */
function normalizeSummary(summary: AgentToolSummary, label: string): AgentToolSummary {
  const entries = Object.entries(summary);
  if (entries.length > 32) {
    throw new RangeError(`Agent 工具${label}摘要最多包含 32 个字段`);
  }
  const normalized: Record<string, string | number | boolean | null> = {};
  for (const [key, value] of entries) {
    if (key.length === 0 || key.length > 64) {
      throw new RangeError(`Agent 工具${label}摘要字段名长度无效`);
    }
    const normalizedKey = key.toLowerCase().replace(/[^a-z0-9]/g, "");
    if (FORBIDDEN_SUMMARY_KEYS.has(normalizedKey)) {
      throw new AgentToolPolicyError(`Agent 工具${label}摘要禁止包含敏感字段: ${key}`);
    }
    if (typeof value === "number" && !Number.isFinite(value)) {
      throw new RangeError(`Agent 工具${label}摘要必须使用有限数值: ${key}`);
    }
    if (typeof value === "string" && value.length > 512) {
      throw new RangeError(`Agent 工具${label}摘要文本过长: ${key}`);
    }
    if (value !== null && !["string", "number", "boolean"].includes(typeof value)) {
      throw new RangeError(`Agent 工具${label}摘要必须是扁平标量: ${key}`);
    }
    normalized[key] = value;
  }
  return normalized;
}

/** 规范必填文本并拒绝空值。 */
function requireText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}

/** 验证工具所关联的规范消息已经持久化在当前会话。 */
function requirePersistedMessage(session: AgentSessionSnapshot, value: string): string {
  const messageId = requireText(value, "Agent 工具关联消息 ID");
  if (!session.messages.some((message) => message.id === messageId)) {
    throw new RangeError("Agent 工具必须关联当前会话中已持久化的消息");
  }
  return messageId;
}

/** 规范 UTC 时间并明确拒绝不可解析值。 */
function normalizeTimestamp(value: string, label: string): string {
  const normalized = requireText(value, label);
  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) {
    throw new RangeError(`${label}无效`);
  }
  return parsed.toISOString();
}
