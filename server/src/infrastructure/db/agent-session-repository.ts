import type {
  AgentTurnCompletionPlan,
  AgentTurnFailurePlan,
  AgentTurnStartPlan,
  AgentProvider,
  AgentSessionSnapshot,
  AgentSessionStatus,
  NormalizedAgentMessage,
} from "../../domain/agent/index.js";
import { ApplicationError } from "../../application/app-error.js";
import type { LyraDatabase } from "./database.js";

const AGENT_PROVIDERS = new Set<AgentProvider>(["genkit"]);
const SESSION_STATUSES = new Set<AgentSessionStatus>([
  "idle", "running", "cancelling", "closed",
]);
const MESSAGE_ROLES = new Set<NormalizedAgentMessage["role"]>([
  "user", "assistant", "tool", "system",
]);
const MESSAGE_FINAL_STATUSES = new Set<NonNullable<NormalizedAgentMessage["status"]>>([
  "completed", "failed", "cancelled",
]);

/**
 * 服务启动且尚未接收请求时恢复被上次进程中断的 Genkit 回合。
 * 已持久化可见文本不丢弃；进行中的工具和消息形成明确失败/取消终态，
 * 会话回到 idle 并追加系统说明。重复启动恢复没有副作用。
 * 调用方必须通过 WriteCoordinator 在开始服务前执行一次。
 */
export async function recoverInterruptedAgentSessions(
  database: LyraDatabase,
  now: Date,
): Promise<number> {
  if (Number.isNaN(now.getTime())) throw new RangeError("Agent 恢复时间无效");
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    const rows = await connection.runAndReadAll(`
      SELECT count(*)::INTEGER AS count FROM agent_sessions
      WHERE provider = 'genkit' AND status IN ('running', 'cancelling')
    `);
    const count = Number(rows.getRowObjectsJson()[0]?.count ?? 0);
    if (count > 0) {
      const params = { now: now.toISOString() };
      await connection.run(`
        UPDATE agent_tool_calls
        SET status = 'cancelled', result_summary = NULL,
            duration_ms = coalesce(duration_ms, 0), error_code = 'AGENT_TOOL_CANCELLED',
            completed_at = greatest(started_at, CAST($now AS TIMESTAMPTZ))
        WHERE status = 'started' AND session_id IN (
          SELECT id FROM agent_sessions
          WHERE provider = 'genkit' AND status IN ('running', 'cancelling')
        )
      `, params);
      await connection.run(`
        UPDATE agent_messages
        SET status = 'failed', error_code = 'AGENT_TURN_FAILED',
            completed_at = greatest(created_at, CAST($now AS TIMESTAMPTZ))
        WHERE status = 'streaming' AND session_id IN (
          SELECT id FROM agent_sessions
          WHERE provider = 'genkit' AND status IN ('running', 'cancelling')
        )
      `, params);
      await connection.run(`
        INSERT INTO agent_messages (
          id, session_id, ordinal, role, provider, status, content, error_code,
          created_at, completed_at
        )
        SELECT uuid(), s.id, coalesce(m.last_ordinal, 0) + 1, 'system', NULL, 'completed',
               '上一回合因服务重启中断。已保存的消息和工具记录保留，你可以重新提问继续。',
               NULL, greatest(s.updated_at, CAST($now AS TIMESTAMPTZ)),
               greatest(s.updated_at, CAST($now AS TIMESTAMPTZ))
        FROM agent_sessions s
        LEFT JOIN (
          SELECT session_id, max(ordinal) AS last_ordinal
          FROM agent_messages GROUP BY session_id
        ) m ON m.session_id = s.id
        WHERE s.provider = 'genkit' AND s.status IN ('running', 'cancelling')
      `, params);
      await connection.run(`
        UPDATE agent_sessions SET status = 'idle', revision = revision + 1,
          last_event_sequence = last_event_sequence + 1,
          updated_at = greatest(updated_at, CAST($now AS TIMESTAMPTZ))
        WHERE provider = 'genkit' AND status IN ('running', 'cancelling')
      `, params);
    }
    await connection.run("COMMIT");
    return count;
  } catch (error) {
    await connection.run("ROLLBACK");
    throw error;
  }
}

/** 编排器恢复回合时需要的规范会话与持久化事件游标。 */
export interface AgentSessionRuntimeRecord {
  deviceId: string;
  snapshot: AgentSessionSnapshot;
  lastEventSequence: number;
}

/**
 * 从 v5 DuckDB schema 重建一个不含隐藏推理的规范 Agent 会话快照。
 *
 * 单条 LEFT JOIN 查询保证 session 与可见 messages 来自同一查询快照；消息按
 * 持久化 ordinal 排序。账户范围保留 Lyra 账户 UUID，后续查询用例再通过
 * `resolveAccountScope` 映射旧账本键。
 *
 * @param database - 已完成 v1–v11 migration 的数据库。
 * @param sessionId - 服务端生成的 Agent 会话 UUID。
 * @returns 会话不存在时返回 null，否则返回规范快照。
 */
export async function loadAgentSession(
  database: LyraDatabase,
  sessionId: string,
): Promise<AgentSessionSnapshot | null> {
  return (await loadAgentSessionRuntime(database, sessionId))?.snapshot ?? null;
}

/**
 * 从同一查询快照恢复规范会话和最后持久化事件序号。
 *
 * `loadAgentSession` 供只需要认证快照的调用方使用；Agent orchestrator 必须使用
 * 本函数生成下一事件，不能把内存缓冲区长度猜作持久化序号。
 */
export async function loadAgentSessionRuntime(
  database: LyraDatabase,
  sessionId: string,
): Promise<AgentSessionRuntimeRecord | null> {
  const normalizedId = sessionId.trim();
  if (normalizedId.length === 0) {
    throw new RangeError("Agent 会话 ID 不能为空");
  }
  const reader = await database.connection.runAndReadAll(`
    SELECT
      CAST(s.id AS VARCHAR) AS session_id,
      CAST(s.device_id AS VARCHAR) AS device_id,
      s.provider,
      s.status AS session_status,
      s.account_scope_kind,
      CAST(s.account_id AS VARCHAR) AS account_id,
      s.ledger_version,
      s.as_of,
      s.revision,
      s.last_event_sequence,
      s.summary,
      CAST(m.id AS VARCHAR) AS message_id,
      m.role AS message_role,
      m.status AS message_status,
      m.content AS message_content,
      m.error_code AS message_error_code,
      m.created_at AS message_created_at
    FROM agent_sessions s
    LEFT JOIN agent_messages m ON m.session_id = s.id
    WHERE s.id = CAST($session_id AS UUID)
    ORDER BY m.ordinal
  `, { session_id: normalizedId });
  const rows = reader.getRowObjectsJson();
  const first = rows[0];
  if (first === undefined) {
    return null;
  }

  const provider = parseProvider(first.provider);
  const status = parseSessionStatus(first.session_status);
  const accountScope = parseAccountScope(first.account_scope_kind, first.account_id);
  const snapshot: AgentSessionSnapshot = {
    id: requireString(first.session_id, "Agent 会话 ID"),
    provider,
    status,
    accountScope,
    ledgerVersion: parseNonNegativeInteger(first.ledger_version, "Agent 账本版本"),
    asOf: parseTimestamp(first.as_of, "Agent 数据截止时间"),
    revision: parsePositiveInteger(first.revision, "Agent 会话 revision"),
    summary: first.summary === null
      ? null
      : requireString(first.summary, "Agent 会话摘要"),
    messages: rows
      .filter((row) => row.message_id !== null)
      .map(parseMessage),
  };
  return {
    deviceId: requireString(first.device_id, "Agent 设备 ID"),
    snapshot,
    lastEventSequence: parseNonNegativeInteger(
      first.last_event_sequence,
      "Agent 最后事件序号",
    ),
  };
}

/**
 * 持久化领域层已经创建的空 Agent 会话。
 *
 * 当前账本版本与设备状态在同一事务中核对；只保存规范会话，不创建子进程、
 * 外部运行环境。调用方应通过 `WriteCoordinator` 串行执行本函数。
 *
 * @param database - 已完成 v1–v11 migration 的数据库。
 * @param input.session - `createAgentSession` 返回的 idle 空会话。
 * @param input.deviceId - 已认证且仍为 active 的配对设备 UUID。
 * @param input.createdAt - 服务端可信创建时间。
 */
export async function persistNewAgentSession(
  database: LyraDatabase,
  input: {
    session: AgentSessionSnapshot;
    deviceId: string;
    createdAt: Date;
  },
): Promise<void> {
  if (
    input.session.status !== "idle"
    || input.session.revision !== 1
    || input.session.messages.length !== 0
  ) {
    throw new RangeError("新 Agent 会话必须是 revision 1 的 idle 空会话");
  }
  const deviceId = requireNonEmptyText(input.deviceId, "Agent 设备 ID");
  if (Number.isNaN(input.createdAt.getTime())) {
    throw new RangeError("Agent 会话创建时间无效");
  }
  const createdAt = input.createdAt.toISOString();
  const asOf = parseTimestamp(input.session.asOf, "Agent 数据截止时间");
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    const currentLedgerVersion = await readCurrentLedgerVersion(database);
    if (currentLedgerVersion !== input.session.ledgerVersion) {
      throw new ApplicationError({
        code: "LEDGER_VERSION_CONFLICT",
        message: "账本版本已变化，请重新创建 Agent 会话",
        statusCode: 409,
        retryable: false,
      });
    }
    await assertActiveDevice(database, deviceId);
    // 消息外键指向不可变身份表；新会话必须同时登记身份行，
    // 之后 agent_sessions 才能在已存在消息时继续更新回合状态。
    await connection.run(`
      INSERT INTO agent_session_identities (id)
      VALUES (CAST($session_id AS UUID))
    `, { session_id: input.session.id });
    await connection.run(`
      INSERT INTO agent_sessions (
        id, device_id, provider, status, account_scope_kind, account_id,
        ledger_version, as_of, revision, last_event_sequence, summary,
        created_at, updated_at
      ) VALUES (
        CAST($session_id AS UUID), CAST($device_id AS UUID), $provider, 'idle',
        $account_scope_kind, CAST($account_id AS UUID), $ledger_version,
        CAST($as_of AS TIMESTAMPTZ), 1, 0, $summary,
        CAST($created_at AS TIMESTAMPTZ), CAST($created_at AS TIMESTAMPTZ)
      )
    `, {
      session_id: input.session.id,
      device_id: deviceId,
      provider: input.session.provider,
      account_scope_kind: input.session.accountScope.kind,
      account_id: input.session.accountScope.kind === "account"
        ? input.session.accountScope.account
        : null,
      ledger_version: input.session.ledgerVersion,
      as_of: asOf,
      summary: input.session.summary ?? null,
      created_at: createdAt,
    });
    await connection.run("COMMIT");
  } catch (error: unknown) {
    try {
      await connection.run("ROLLBACK");
    } catch (rollbackError: unknown) {
      throw new AggregateError([error, rollbackError], "Agent 会话创建失败且回滚失败");
    }
    throw error;
  }
}

/**
 * 原子固化用户消息、running 状态、revision 和下一事件序号。
 *
 * Genkit 执行器只能在本函数提交成功后收到 `plan.adapterInput`。事件正文不落库，
 * 仅持久化序号；提交后由 orchestrator 把领域事件写入有界内存缓冲并推送 App。
 * 同一计划重放或会话已变化时 CAS 失败，事务不会留下重复消息。
 *
 * @param database - 单一服务进程持有的 DuckDB。
 * @param plan - `startAgentTurn` 生成的确定性回合起始计划。
 */
export async function persistAgentTurnStart(
  database: LyraDatabase,
  plan: AgentTurnStartPlan,
): Promise<void> {
  assertTurnStartPlan(plan);
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    const accountId = plan.session.accountScope.kind === "account"
      ? plan.session.accountScope.account
      : null;
    const updated = await connection.run(`
      UPDATE agent_sessions
      SET status = 'running',
          revision = $next_revision,
          last_event_sequence = $next_event_sequence,
          updated_at = CAST($started_at AS TIMESTAMPTZ)
      WHERE id = CAST($session_id AS UUID)
        AND status = 'idle'
        AND provider = $provider
        AND revision = $expected_revision
        AND last_event_sequence = $expected_event_sequence
        AND ledger_version = $ledger_version
        AND as_of = CAST($as_of AS TIMESTAMPTZ)
        AND account_scope_kind = $account_scope_kind
        AND (
          ($account_scope_kind = 'all' AND account_id IS NULL)
          OR ($account_scope_kind = 'account' AND account_id = CAST($account_id AS UUID))
        )
    `, {
      next_revision: plan.session.revision,
      next_event_sequence: plan.event.sequence,
      started_at: plan.userMessage.createdAt,
      session_id: plan.session.id,
      provider: plan.session.provider,
      expected_revision: plan.session.revision - 1,
      expected_event_sequence: plan.event.sequence - 1,
      ledger_version: plan.session.ledgerVersion,
      as_of: plan.session.asOf,
      account_scope_kind: plan.session.accountScope.kind,
      account_id: accountId,
    });
    if (updated.rowsChanged !== 1) {
      throw new ApplicationError({
        code: "AGENT_SESSION_STATE_CONFLICT",
        message: "Agent 会话已变化，请刷新后重试",
        statusCode: 409,
        retryable: false,
      });
    }
    await connection.run(`
      INSERT INTO agent_messages (
        id, session_id, ordinal, role, provider, status, content, error_code,
        created_at, completed_at
      ) VALUES (
        CAST($message_id AS UUID), CAST($session_id AS UUID), $ordinal, 'user',
        NULL, 'completed', $content, NULL,
        CAST($created_at AS TIMESTAMPTZ), CAST($created_at AS TIMESTAMPTZ)
      )
    `, {
      message_id: plan.userMessage.id,
      session_id: plan.session.id,
      ordinal: plan.session.messages.length,
      content: plan.userMessage.content,
      created_at: plan.userMessage.createdAt,
    });
    await connection.run("COMMIT");
  } catch (error: unknown) {
    try {
      await connection.run("ROLLBACK");
    } catch (rollbackError: unknown) {
      throw new AggregateError([error, rollbackError], "Agent 回合启动失败且回滚失败");
    }
    throw error;
  }
}

/** 原子写入完整助手回答并把 running 会话恢复到 idle。 */
export async function persistAgentTurnCompletion(
  database: LyraDatabase,
  plan: AgentTurnCompletionPlan,
): Promise<void> {
  await persistAgentTurnSettlement(database, {
    session: plan.session,
    assistantMessage: plan.assistantMessage,
    event: plan.event,
    expectedStatus: "running",
    expectedEventType: "message.completed",
  });
}

/**
 * 原子写入失败/取消的可见部分并恢复 idle。
 *
 * 空部分回答不会伪造助手消息；`failed` 从 running 收口，`cancelled` 只从已经
 * 持久化的 cancelling 状态收口。底层供应商错误不属于领域计划，因此不会进入表。
 */
export async function persistAgentTurnFailure(
  database: LyraDatabase,
  plan: AgentTurnFailurePlan,
): Promise<void> {
  const outcome = plan.event.payload["outcome"];
  if (outcome !== "failed" && outcome !== "cancelled") {
    throw new RangeError("Agent 回合失败计划缺少稳定终态");
  }
  await persistAgentTurnSettlement(database, {
    session: plan.session,
    assistantMessage: plan.assistantMessage,
    event: plan.event,
    expectedStatus: outcome === "failed" ? "running" : "cancelling",
    expectedEventType: "message.failed",
  });
}

/**
 * 把 running 会话 CAS 固化为 cancelling，记录用户取消请求。
 *
 * 只推进状态与 revision，不追加消息或事件；取消意图由中止的供应商适配器以
 * `message_failed(cancelled)` 流信号呈现，随后仍由失败收口事务恢复 idle。
 *
 * @param database - 单一服务进程持有的 DuckDB。
 * @param input.session - `transitionAgentSession` 生成的 cancelling 快照。
 */
export async function persistAgentSessionCancelling(
  database: LyraDatabase,
  input: {
    session: AgentSessionSnapshot;
    cancelledAt: string;
  },
): Promise<void> {
  if (input.session.status !== "cancelling" || input.session.revision <= 1) {
    throw new RangeError("Agent 取消计划必须是递增 revision 的 cancelling 快照");
  }
  const cancelledAt = parseTimestamp(input.cancelledAt, "Agent 取消时间");
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    const accountId = input.session.accountScope.kind === "account"
      ? input.session.accountScope.account
      : null;
    const updated = await connection.run(`
      UPDATE agent_sessions
      SET status = 'cancelling',
          revision = $next_revision,
          updated_at = CAST($cancelled_at AS TIMESTAMPTZ)
      WHERE id = CAST($session_id AS UUID)
        AND status = 'running'
        AND provider = $provider
        AND revision = $expected_revision
        AND ledger_version = $ledger_version
        AND as_of = CAST($as_of AS TIMESTAMPTZ)
        AND account_scope_kind = $account_scope_kind
        AND (
          ($account_scope_kind = 'all' AND account_id IS NULL)
          OR ($account_scope_kind = 'account' AND account_id = CAST($account_id AS UUID))
        )
    `, {
      next_revision: input.session.revision,
      cancelled_at: cancelledAt,
      session_id: input.session.id,
      provider: input.session.provider,
      expected_revision: input.session.revision - 1,
      ledger_version: input.session.ledgerVersion,
      as_of: input.session.asOf,
      account_scope_kind: input.session.accountScope.kind,
      account_id: accountId,
    });
    if (updated.rowsChanged !== 1) {
      throw new ApplicationError({
        code: "AGENT_SESSION_STATE_CONFLICT",
        message: "Agent 会话已变化，取消请求未生效",
        statusCode: 409,
        retryable: false,
      });
    }
    await connection.run("COMMIT");
  } catch (error: unknown) {
    try {
      await connection.run("ROLLBACK");
    } catch (rollbackError: unknown) {
      throw new AggregateError([error, rollbackError], "Agent 取消持久化失败且回滚失败");
    }
    throw error;
  }
}

/** 成功与失败共用的持久化终态输入。 */
interface AgentTurnSettlementInput {
  session: AgentSessionSnapshot;
  assistantMessage: NormalizedAgentMessage | null;
  event: AgentTurnCompletionPlan["event"];
  expectedStatus: "running" | "cancelling";
  expectedEventType: "message.completed" | "message.failed";
}

/**
 * 以一次 CAS 和可选助手消息插入完成回合终态事务。
 *
 * 事件序号采用单调推进：流式 delta 只存在于内存与客户端事件流，不逐条落库，
 * 因此持久化序号允许跳过这些临时事件，但绝不允许回退或重复收敛。
 */
async function persistAgentTurnSettlement(
  database: LyraDatabase,
  input: AgentTurnSettlementInput,
): Promise<void> {
  assertTurnSettlement(input);
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    const accountId = input.session.accountScope.kind === "account"
      ? input.session.accountScope.account
      : null;
    const updated = await connection.run(`
      UPDATE agent_sessions
      SET status = 'idle',
          revision = $next_revision,
          last_event_sequence = $next_event_sequence,
          updated_at = CAST($finished_at AS TIMESTAMPTZ)
      WHERE id = CAST($session_id AS UUID)
        AND status = $expected_status
        AND provider = $provider
        AND revision = $expected_revision
        AND last_event_sequence < $next_event_sequence
        AND ledger_version = $ledger_version
        AND as_of = CAST($as_of AS TIMESTAMPTZ)
        AND account_scope_kind = $account_scope_kind
        AND (
          ($account_scope_kind = 'all' AND account_id IS NULL)
          OR ($account_scope_kind = 'account' AND account_id = CAST($account_id AS UUID))
        )
    `, {
      next_revision: input.session.revision,
      next_event_sequence: input.event.sequence,
      finished_at: input.event.occurredAt,
      session_id: input.session.id,
      expected_status: input.expectedStatus,
      provider: input.session.provider,
      expected_revision: input.session.revision - 1,
      ledger_version: input.session.ledgerVersion,
      as_of: input.session.asOf,
      account_scope_kind: input.session.accountScope.kind,
      account_id: accountId,
    });
    if (updated.rowsChanged !== 1) {
      throw new ApplicationError({
        code: "AGENT_SESSION_STATE_CONFLICT",
        message: "Agent 回合状态已变化，请刷新后重试",
        statusCode: 409,
        retryable: false,
      });
    }
    if (input.assistantMessage !== null) {
      const messageStatus = input.assistantMessage.status;
      if (messageStatus === undefined) {
        throw new RangeError("Agent 助手终态消息缺少状态");
      }
      await connection.run(`
        INSERT INTO agent_messages (
          id, session_id, ordinal, role, provider, status, content, error_code,
          created_at, completed_at
        ) VALUES (
          CAST($message_id AS UUID), CAST($session_id AS UUID), $ordinal,
          'assistant', $provider, $status, $content, $error_code,
          CAST($created_at AS TIMESTAMPTZ), CAST($created_at AS TIMESTAMPTZ)
        )
      `, {
        message_id: input.assistantMessage.id,
        session_id: input.session.id,
        ordinal: input.session.messages.length,
        provider: input.session.provider,
        status: messageStatus,
        content: input.assistantMessage.content,
        error_code: input.assistantMessage.errorCode ?? null,
        created_at: input.assistantMessage.createdAt,
      });
    }
    await connection.run("COMMIT");
  } catch (error: unknown) {
    try {
      await connection.run("ROLLBACK");
    } catch (rollbackError: unknown) {
      throw new AggregateError([error, rollbackError], "Agent 回合收口失败且回滚失败");
    }
    throw error;
  }
}

/** 校验领域终态计划没有被调用方重新拼装或篡改关联字段。 */
function assertTurnSettlement(input: AgentTurnSettlementInput): void {
  const message = input.assistantMessage;
  if (
    input.session.status !== "idle"
    || input.event.type !== input.expectedEventType
    || input.event.sessionId !== input.session.id
    || input.event.occurredAt.trim().length === 0
    || input.session.revision <= 1
    || input.event.sequence <= 0
    || (message !== null && (
      message.role !== "assistant"
      || input.event.messageId !== message.id
      || input.session.messages.at(-1)?.id !== message.id
      || message.status === undefined
    ))
  ) {
    throw new RangeError("Agent 回合终态计划与规范会话不一致");
  }
  if (message === null && input.expectedEventType !== "message.failed") {
    throw new RangeError("Agent 成功回合不能缺少助手消息");
  }
  if (
    message !== null
    && (
      (input.expectedEventType === "message.completed" && message.status !== "completed")
      || (
        input.expectedEventType === "message.failed"
        && message.status !== input.event.payload["outcome"]
      )
    )
  ) {
    throw new RangeError("Agent 助手消息状态与回合终态不一致");
  }
  parseTimestamp(input.event.occurredAt, "Agent 回合结束时间");
}

/** 回合计划必须仍保持领域层生成的消息、事件和会话关联。 */
function assertTurnStartPlan(plan: AgentTurnStartPlan): void {
  if (
    plan.session.status !== "running"
    || plan.userMessage.role !== "user"
    || plan.event.type !== "message.started"
    || plan.event.sessionId !== plan.session.id
    || plan.event.messageId !== plan.userMessage.id
    || plan.event.occurredAt !== plan.userMessage.createdAt
    || plan.session.messages.at(-1)?.id !== plan.userMessage.id
    || plan.adapterInput.messageId !== plan.userMessage.id
    || plan.adapterInput.content !== plan.userMessage.content
    || plan.session.revision <= 1
    || plan.event.sequence <= 0
  ) {
    throw new RangeError("Agent 回合启动计划与规范会话不一致");
  }
  parseTimestamp(plan.userMessage.createdAt, "Agent 回合开始时间");
}

/** 新会话只能绑定当前账本版本。 */
export async function readCurrentLedgerVersion(database: LyraDatabase): Promise<number> {
  const reader = await database.connection.runAndReadAll(
    "SELECT coalesce(max(version), 0) AS version FROM ledger_versions",
  );
  return parseNonNegativeInteger(reader.getRowObjectsJson()[0]?.version, "当前账本版本");
}

/** 新会话只能绑定活跃账户；未知或已归档账户明确拒绝，不进入外键错误。 */
export async function assertActiveAccountExists(
  database: LyraDatabase,
  accountId: string,
): Promise<void> {
  const normalizedId = requireNonEmptyText(accountId, "Agent 账户 ID");
  const reader = await database.connection.runAndReadAll(`
    SELECT status
    FROM accounts
    WHERE id = CAST($account_id AS UUID)
  `, { account_id: normalizedId });
  const status = reader.getRowObjectsJson()[0]?.status;
  if (status !== "active") {
    throw new ApplicationError({
      code: "ACCOUNT_SCOPE_REQUIRED",
      message: status === "archived" ? "目标账户已归档" : "目标账户不存在",
      statusCode: 409,
      retryable: false,
    });
  }
}

/** 会话只能在 idle 回合边界显式关闭；closed 会话不可恢复。 */
export async function persistAgentSessionClosed(
  database: LyraDatabase,
  input: {
    session: AgentSessionSnapshot;
    closedAt: string;
  },
): Promise<void> {
  if (input.session.status !== "closed" || input.session.revision <= 1) {
    throw new RangeError("Agent 关闭计划必须是递增 revision 的 closed 快照");
  }
  const closedAt = parseTimestamp(input.closedAt, "Agent 关闭时间");
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    const accountId = input.session.accountScope.kind === "account"
      ? input.session.accountScope.account
      : null;
    const updated = await connection.run(`
      UPDATE agent_sessions
      SET status = 'closed',
          revision = $next_revision,
          closed_at = CAST($closed_at AS TIMESTAMPTZ),
          updated_at = CAST($closed_at AS TIMESTAMPTZ)
      WHERE id = CAST($session_id AS UUID)
        AND status = 'idle'
        AND provider = $provider
        AND revision = $expected_revision
        AND ledger_version = $ledger_version
        AND as_of = CAST($as_of AS TIMESTAMPTZ)
        AND account_scope_kind = $account_scope_kind
        AND (
          ($account_scope_kind = 'all' AND account_id IS NULL)
          OR ($account_scope_kind = 'account' AND account_id = CAST($account_id AS UUID))
        )
    `, {
      next_revision: input.session.revision,
      closed_at: closedAt,
      session_id: input.session.id,
      provider: input.session.provider,
      expected_revision: input.session.revision - 1,
      ledger_version: input.session.ledgerVersion,
      as_of: input.session.asOf,
      account_scope_kind: input.session.accountScope.kind,
      account_id: accountId,
    });
    if (updated.rowsChanged !== 1) {
      throw new ApplicationError({
        code: "AGENT_SESSION_STATE_CONFLICT",
        message: "Agent 会话已变化，关闭请求未生效",
        statusCode: 409,
        retryable: false,
      });
    }
    await connection.run("COMMIT");
  } catch (error: unknown) {
    try {
      await connection.run("ROLLBACK");
    } catch (rollbackError: unknown) {
      throw new AggregateError([error, rollbackError], "Agent 会话关闭失败且回滚失败");
    }
    throw error;
  }
}

/** 会话创建时设备必须仍处于 active，撤销设备不得产生新会话。 */
async function assertActiveDevice(database: LyraDatabase, deviceId: string): Promise<void> {
  const reader = await database.connection.runAndReadAll(`
    SELECT status
    FROM paired_devices
    WHERE id = CAST($device_id AS UUID)
  `, { device_id: deviceId });
  if (reader.getRowObjectsJson()[0]?.status !== "active") {
    throw new ApplicationError({
      code: "DEVICE_REVOKED",
      message: "设备已撤销或不存在",
      statusCode: 403,
      retryable: false,
    });
  }
}

/** 从 JOIN 行重建一条规范可见消息。 */
function parseMessage(row: Readonly<Record<string, unknown>>): NormalizedAgentMessage {
  const role = parseMessageRole(row.message_role);
  const message: NormalizedAgentMessage = {
    id: requireString(row.message_id, "Agent 消息 ID"),
    role,
    content: requireString(row.message_content, "Agent 消息内容"),
    createdAt: parseTimestamp(row.message_created_at, "Agent 消息创建时间"),
  };
  if (role === "assistant") {
    const status = parseMessageFinalStatus(row.message_status);
    message.status = status;
    message.errorCode = row.message_error_code === null
      ? null
      : requireString(row.message_error_code, "Agent 消息错误码");
  }
  return message;
}

/** 解析单账户 UUID 或 all，拒绝违反 schema 语义的组合。 */
function parseAccountScope(
  kind: unknown,
  accountId: unknown,
): AgentSessionSnapshot["accountScope"] {
  if (kind === "all" && accountId === null) {
    return { kind: "all" };
  }
  if (kind === "account" && typeof accountId === "string" && accountId.length > 0) {
    return { kind: "account", account: accountId };
  }
  throw new Error("Agent 会话账户范围与数据库 schema 不一致");
}

/** 收窄数据库 provider 闭集。 */
function parseProvider(value: unknown): AgentProvider {
  if (typeof value === "string" && AGENT_PROVIDERS.has(value as AgentProvider)) {
    return value as AgentProvider;
  }
  throw new Error("Agent 会话 provider 与数据库 schema 不一致");
}

/** 收窄数据库 session 状态闭集。 */
function parseSessionStatus(value: unknown): AgentSessionStatus {
  if (typeof value === "string" && SESSION_STATUSES.has(value as AgentSessionStatus)) {
    return value as AgentSessionStatus;
  }
  throw new Error("Agent 会话状态与数据库 schema 不一致");
}

/** 收窄数据库消息角色闭集。 */
function parseMessageRole(value: unknown): NormalizedAgentMessage["role"] {
  if (
    typeof value === "string"
    && MESSAGE_ROLES.has(value as NormalizedAgentMessage["role"])
  ) {
    return value as NormalizedAgentMessage["role"];
  }
  throw new Error("Agent 消息角色与数据库 schema 不一致");
}

/** streaming 助手内容不是规范快照，必须由回合终态流程收口后再读取。 */
function parseMessageFinalStatus(
  value: unknown,
): NonNullable<NormalizedAgentMessage["status"]> {
  if (
    typeof value === "string"
    && MESSAGE_FINAL_STATUSES.has(value as NonNullable<NormalizedAgentMessage["status"]>)
  ) {
    return value as NonNullable<NormalizedAgentMessage["status"]>;
  }
  throw new Error("Agent 助手消息尚未形成可持久化终态");
}

/** DuckDB BIGINT JSON 值统一收窄为非负安全整数。 */
function parseNonNegativeInteger(value: unknown, label: string): number {
  const parsed = typeof value === "number"
    ? value
    : typeof value === "string" && /^\d+$/u.test(value)
      ? Number(value)
      : Number.NaN;
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new RangeError(`${label} 必须是非负安全整数`);
  }
  return parsed;
}

/** revision 必须严格为正。 */
function parsePositiveInteger(value: unknown, label: string): number {
  const parsed = parseNonNegativeInteger(value, label);
  if (parsed === 0) {
    throw new RangeError(`${label} 必须为正整数`);
  }
  return parsed;
}

/** 把 DuckDB TIMESTAMPTZ JSON 文本规范为 UTC ISO。 */
function parseTimestamp(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new RangeError(`${label} 必须是时间字符串`);
  }
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) {
    throw new RangeError(`${label} 无效`);
  }
  return timestamp.toISOString();
}

/** 读取必填非空字符串。 */
function requireString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`${label} 与数据库 schema 不一致`);
  }
  return value;
}

/** 规范调用边界的必填文本。 */
function requireNonEmptyText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}
