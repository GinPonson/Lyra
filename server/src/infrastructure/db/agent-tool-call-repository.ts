import type {
  AgentToolCallCompletionPlan,
  AgentToolCallRecord,
  AgentToolCallRejectionPlan,
  AgentToolCallStartPlan,
} from "../../domain/agent/index.js";
import { ApplicationError } from "../../application/app-error.js";
import type { LyraDatabase } from "./database.js";

const TOOL_CALL_STATUSES = new Set<AgentToolCallRecord["status"]>([
  "started", "completed", "failed", "cancelled", "rejected",
]);
const TOOL_AUTHORIZATION_DECISIONS = new Set<
  AgentToolCallRecord["authorizationDecision"]
>(["allowed", "denied"]);
const TOOL_ERROR_CODES = new Set<NonNullable<AgentToolCallRecord["errorCode"]>>([
  "AGENT_TOOL_EXECUTION_FAILED", "AGENT_TOOL_CANCELLED", "AGENT_TOOL_FORBIDDEN",
]);

/** 会话写侧统一使用的稳定状态冲突。 */
function sessionConflict(message: string): ApplicationError {
  return new ApplicationError({
    code: "AGENT_SESSION_STATE_CONFLICT",
    message,
    statusCode: 409,
    retryable: false,
  });
}

/**
 * 原子持久化 started 工具调用并推进会话事件序号。
 *
 * 工具行通过复合外键绑定已持久化消息；事件序号 CAS 保证只有持有当前序号的
 * orchestrator 可以写入，重复计划或过期编排实例整体回滚。
 *
 * @param database - 单一服务进程持有的 DuckDB。
 * @param plan - `startAgentToolCall` 生成的确定性开始计划。
 */
export async function persistAgentToolCallStart(
  database: LyraDatabase,
  plan: AgentToolCallStartPlan,
): Promise<void> {
  assertToolCallPlan(plan.call, plan.event, "tool.started");
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    await advanceSessionEventSequence(connection, {
      sessionId: plan.call.sessionId,
      nextSequence: plan.event.sequence,
    });
    await connection.run(`
      INSERT INTO agent_tool_calls (
        id, session_id, message_id, ordinal, tool_name, authorization_decision,
        status, arguments_summary, result_summary, ledger_version, duration_ms,
        error_code, started_at, completed_at
      ) VALUES (
        CAST($call_id AS UUID), CAST($session_id AS UUID), CAST($message_id AS UUID),
        $ordinal, $tool_name, 'allowed', 'started', CAST($arguments AS JSON),
        NULL, $ledger_version, NULL, NULL, CAST($started_at AS TIMESTAMPTZ), NULL
      )
    `, {
      call_id: plan.call.id,
      session_id: plan.call.sessionId,
      message_id: plan.call.messageId,
      ordinal: plan.call.ordinal,
      tool_name: plan.call.toolName,
      arguments: JSON.stringify(plan.call.argumentsSummary),
      ledger_version: plan.call.ledgerVersion,
      started_at: plan.call.startedAt,
    });
    await connection.run("COMMIT");
  } catch (error: unknown) {
    await rollbackTurnWrite(connection, error, "Agent 工具调用启动失败且回滚失败");
  }
}

/**
 * 以 CAS 把 started 工具调用固化为终态并推进事件序号。
 *
 * 成功写入有界结果摘要；失败与取消只写稳定错误码，不保存部分结果。终态不可
 * 重复收敛：重复完成或会话序号漂移都会整体回滚。
 *
 * @param database - 单一服务进程持有的 DuckDB。
 * @param plan - `completeAgentToolCall` 生成的确定性终态计划。
 */
export async function persistAgentToolCallCompletion(
  database: LyraDatabase,
  plan: AgentToolCallCompletionPlan,
): Promise<void> {
  assertToolCallPlan(plan.call, plan.event, "tool.completed");
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    const updated = await connection.run(`
      UPDATE agent_tool_calls
      SET status = $status,
          result_summary = CAST($result_summary AS JSON),
          duration_ms = $duration_ms,
          error_code = $error_code,
          completed_at = CAST($completed_at AS TIMESTAMPTZ)
      WHERE id = CAST($call_id AS UUID)
        AND session_id = CAST($session_id AS UUID)
        AND status = 'started'
    `, {
      status: plan.call.status,
      result_summary: plan.call.resultSummary === null
        ? null
        : JSON.stringify(plan.call.resultSummary),
      duration_ms: plan.call.durationMs,
      error_code: plan.call.errorCode,
      completed_at: plan.call.completedAt,
      call_id: plan.call.id,
      session_id: plan.call.sessionId,
    });
    if (updated.rowsChanged !== 1) {
      throw sessionConflict("Agent 工具调用已变化，无法重复收敛终态");
    }
    await advanceSessionEventSequence(connection, {
      sessionId: plan.call.sessionId,
      nextSequence: plan.event.sequence,
    });
    await connection.run("COMMIT");
  } catch (error: unknown) {
    await rollbackTurnWrite(connection, error, "Agent 工具调用收口失败且回滚失败");
  }
}

/**
 * 原子持久化策略拒绝的 rejected 工具调用、事件序号与最小审计事件。
 *
 * 拒绝没有 started 前置状态；请求参数不进入任何记录。审计事件与调用记录在
 * 同一事务落库，保证"有拒绝记录必有审计痕迹"。
 *
 * @param database - 单一服务进程持有的 DuckDB。
 * @param plan - `rejectAgentToolCall` 生成的确定性拒绝计划。
 */
export async function persistAgentToolCallRejection(
  database: LyraDatabase,
  plan: AgentToolCallRejectionPlan,
): Promise<void> {
  assertToolCallPlan(plan.call, plan.event, "tool.completed");
  if (plan.call.status !== "rejected" || plan.call.authorizationDecision !== "denied") {
    throw new RangeError("Agent 工具拒绝计划必须是 denied/rejected 终态");
  }
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    await advanceSessionEventSequence(connection, {
      sessionId: plan.call.sessionId,
      nextSequence: plan.event.sequence,
    });
    await connection.run(`
      INSERT INTO agent_tool_calls (
        id, session_id, message_id, ordinal, tool_name, authorization_decision,
        status, arguments_summary, result_summary, ledger_version, duration_ms,
        error_code, started_at, completed_at
      ) VALUES (
        CAST($call_id AS UUID), CAST($session_id AS UUID), CAST($message_id AS UUID),
        $ordinal, $tool_name, 'denied', 'rejected', CAST('{}' AS JSON),
        NULL, $ledger_version, $duration_ms, $error_code,
        CAST($rejected_at AS TIMESTAMPTZ), CAST($rejected_at AS TIMESTAMPTZ)
      )
    `, {
      call_id: plan.call.id,
      session_id: plan.call.sessionId,
      message_id: plan.call.messageId,
      ordinal: plan.call.ordinal,
      tool_name: plan.call.toolName,
      ledger_version: plan.call.ledgerVersion,
      duration_ms: plan.call.durationMs,
      error_code: plan.call.errorCode,
      rejected_at: plan.call.startedAt,
    });
    await connection.run(`
      INSERT INTO audit_events (
        id, occurred_at, event_type, outcome, actor_type, actor_id, request_id,
        target_type, target_id, account_id, ledger_version, error_code, metadata_json
      ) VALUES (
        CAST($event_id AS UUID), CAST($occurred_at AS TIMESTAMPTZ), $event_type,
        $outcome, $actor_type, NULL, $request_id, $target_type, $target_id,
        CAST($account_id AS UUID), $ledger_version, $error_code,
        CAST($metadata AS JSON)
      )
    `, {
      event_id: plan.auditEvent.id,
      occurred_at: plan.auditEvent.occurredAt,
      event_type: plan.auditEvent.eventType,
      outcome: plan.auditEvent.outcome,
      actor_type: plan.auditEvent.actorType,
      request_id: plan.auditEvent.requestId,
      target_type: plan.auditEvent.targetType,
      target_id: plan.auditEvent.targetId,
      account_id: plan.auditEvent.accountId,
      ledger_version: plan.auditEvent.ledgerVersion,
      error_code: plan.auditEvent.errorCode,
      metadata: JSON.stringify(plan.auditEvent.metadata),
    });
    await connection.run("COMMIT");
  } catch (error: unknown) {
    await rollbackTurnWrite(connection, error, "Agent 工具拒绝持久化失败且回滚失败");
  }
}

/** 校验计划没有被调用方重新拼装或篡改会话、消息与事件关联。 */
function assertToolCallPlan(
  call: AgentToolCallStartPlan["call"],
  event: AgentToolCallStartPlan["event"],
  expectedEventType: "tool.started" | "tool.completed",
): void {
  if (
    event.type !== expectedEventType
    || event.sessionId !== call.sessionId
    || event.messageId !== call.messageId
    || event.occurredAt.trim().length === 0
    || event.sequence <= 0
    || call.ordinal <= 0
    || call.ledgerVersion < 0
    || call.startedAt.trim().length === 0
  ) {
    throw new RangeError("Agent 工具调用计划与规范会话不一致");
  }
}

/**
 * 以单调 CAS 把会话事件序号推进到计划序号。
 *
 * 流式 delta 只推进内存计数器，不逐条落库；持久化序号因此允许跳过临时事件，
 * 但重复写入（序号不高于已持久化位置）一律冲突，不产生回退或重复序号。
 */
async function advanceSessionEventSequence(
  connection: LyraDatabase["connection"],
  input: { sessionId: string; nextSequence: number },
): Promise<void> {
  const updated = await connection.run(`
    UPDATE agent_sessions
    SET last_event_sequence = $next_sequence
    WHERE id = CAST($session_id AS UUID)
      AND status = 'running'
      AND last_event_sequence < $next_sequence
  `, {
    next_sequence: input.nextSequence,
    session_id: input.sessionId,
  });
  if (updated.rowsChanged !== 1) {
    throw sessionConflict("Agent 会话已变化，无法推进事件序号");
  }
}

/** 回滚写事务；回滚自身失败时聚合原始错误一起抛出。 */
async function rollbackTurnWrite(
  connection: LyraDatabase["connection"],
  originalError: unknown,
  message: string,
): Promise<never> {
  try {
    await connection.run("ROLLBACK");
  } catch (rollbackError: unknown) {
    throw new AggregateError([originalError, rollbackError], message);
  }
  throw originalError;
}

/** 规范调用边界的必填文本；空文本意味着数据库形态与预期不符。 */
function requireToolText(value: unknown, label: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new RangeError(`${label} 与数据库 schema 不一致`);
  }
  return value;
}

/** 把 DuckDB JSON 行归一为扁平摘要对象。 */
function parseToolSummary(value: unknown): Record<string, string | number | boolean | null> {
  if (value === null || value === undefined) {
    return {};
  }
  if (typeof value !== "string") {
    throw new RangeError("Agent 工具摘要必须是 JSON 文本");
  }
  const parsed: unknown = JSON.parse(value);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new RangeError("Agent 工具摘要必须是 JSON 对象");
  }
  const summary: Record<string, string | number | boolean | null> = {};
  for (const [key, entry] of Object.entries(parsed)) {
    if (
      entry === null
      || typeof entry === "string"
      || typeof entry === "number"
      || typeof entry === "boolean"
    ) {
      summary[key] = entry;
    }
  }
  return summary;
}

/** 把 TIMESTAMPTZ JSON 文本规范为 UTC ISO。 */
function parseToolTimestamp(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new RangeError(`${label} 必须是时间字符串`);
  }
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) {
    throw new RangeError(`${label} 无效`);
  }
  return timestamp.toISOString();
}

/**
 * 读取单个 Agent 会话内的全部规范工具调用记录。
 *
 * 只做只读投影：状态与错误码命中闭域集合，摘要保持领域层验证后的扁平
 * 有界形态；会话不存在时返回 null，供路由层决定 404。
 *
 * @param database - 单一服务进程持有的 DuckDB。
 * @param sessionId - 已认证请求中的会话 UUID。
 */
export async function loadAgentToolCalls(
  database: LyraDatabase,
  sessionId: string,
): Promise<readonly AgentToolCallRecord[] | null> {
  const normalizedId = sessionId.trim();
  if (normalizedId.length === 0) {
    throw new RangeError("Agent 会话 ID 不能为空");
  }
  const reader = await database.connection.runAndReadAll(`
    SELECT
      CAST(t.id AS VARCHAR) AS call_id,
      CAST(t.session_id AS VARCHAR) AS session_id,
      CAST(t.message_id AS VARCHAR) AS message_id,
      t.ordinal,
      t.tool_name,
      t.authorization_decision,
      t.status,
      t.arguments_summary,
      t.result_summary,
      t.ledger_version,
      t.duration_ms,
      t.error_code,
      t.started_at,
      t.completed_at
    FROM agent_tool_calls t
    WHERE t.session_id = CAST($session_id AS UUID)
    ORDER BY t.started_at, t.ordinal, t.id
  `, { session_id: normalizedId });
  const rows = reader.getRowObjectsJson();
  if (rows.length === 0) {
    // 会话本身可能不存在，也可能只是没有工具调用；用身份表区分。
    const session = await database.connection.runAndReadAll(`
      SELECT 1 AS present
      FROM agent_sessions s
      WHERE s.id = CAST($session_id AS UUID)
    `, { session_id: normalizedId });
    if (session.getRowObjectsJson().length === 0) {
      return null;
    }
    return [];
  }
  return rows.map((row): AgentToolCallRecord => {
    const statusValue = requireToolText(row.status, "Agent 工具调用状态");
    if (!TOOL_CALL_STATUSES.has(statusValue as AgentToolCallRecord["status"])) {
      throw new RangeError(`Agent 工具调用状态越界: ${statusValue}`);
    }
    const decisionValue = requireToolText(
      row.authorization_decision,
      "Agent 工具授权判定",
    );
    if (
      !TOOL_AUTHORIZATION_DECISIONS.has(
        decisionValue as AgentToolCallRecord["authorizationDecision"],
      )
    ) {
      throw new RangeError(`Agent 工具授权判定越界: ${decisionValue}`);
    }
    const errorCodeValue = row.error_code === null
      ? null
      : requireToolText(row.error_code, "Agent 工具错误码");
    if (
      errorCodeValue !== null
      && !TOOL_ERROR_CODES.has(
        errorCodeValue as NonNullable<AgentToolCallRecord["errorCode"]>,
      )
    ) {
      throw new RangeError(`Agent 工具错误码越界: ${errorCodeValue}`);
    }
    return {
      id: requireToolText(row.call_id, "Agent 工具调用 ID"),
      sessionId: requireToolText(row.session_id, "Agent 会话 ID"),
      messageId: requireToolText(row.message_id, "Agent 消息 ID"),
      ordinal: Number(row.ordinal),
      toolName: requireToolText(row.tool_name, "Agent 工具名"),
      authorizationDecision:
        decisionValue as AgentToolCallRecord["authorizationDecision"],
      status: statusValue as AgentToolCallRecord["status"],
      argumentsSummary: parseToolSummary(row.arguments_summary),
      resultSummary: row.result_summary === null
        ? null
        : parseToolSummary(row.result_summary),
      ledgerVersion: Number(row.ledger_version),
      durationMs: row.duration_ms === null ? null : Number(row.duration_ms),
      errorCode:
        errorCodeValue as AgentToolCallRecord["errorCode"],
      startedAt: parseToolTimestamp(row.started_at, "Agent 工具开始时间"),
      completedAt: row.completed_at === null
        ? null
        : parseToolTimestamp(row.completed_at, "Agent 工具完成时间"),
    };
  });
}
