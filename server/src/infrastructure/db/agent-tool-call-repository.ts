import type {
  AgentToolCallCompletionPlan,
  AgentToolCallRejectionPlan,
  AgentToolCallStartPlan,
} from "../../domain/agent/index.js";
import { ApplicationError } from "../../application/app-error.js";
import type { LyraDatabase } from "./database.js";

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
