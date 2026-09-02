import { randomUUID } from "node:crypto";

import type {
  AgentProvider,
  AgentProviderAvailability,
  AgentSessionSnapshot,
  AgentSwitchPlan,
} from "../domain/agent/index.js";
import {
  AgentSwitchError,
  createAgentSession,
  prepareAgentSwitch,
  transitionAgentSession,
} from "../domain/agent/index.js";
import type { AgentAvailabilitySnapshot } from "./agent-availability.js";
import type { AgentTurnOrchestrator } from "./agent-turn-orchestrator.js";
import { ApplicationError } from "./app-error.js";
import type { WriteCoordinator } from "./write-coordinator.js";
import {
  assertActiveAccountExists,
  persistAgentSessionClosed,
  persistAgentSessionSwitch,
  persistNewAgentSession,
  readCurrentLedgerVersion,
} from "../infrastructure/db/agent-session-repository.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";

/** 会话创建请求的显式输入；账本版本与截止时间由服务端权威生成。 */
export interface CreateAgentSessionRequest {
  provider: AgentProvider;
  accountScope: { kind: "all" } | { kind: "account"; account: string };
}

/**
 * 创建并持久化一个新的 Agent 会话，同时让 orchestrator 进入可编排状态。
 *
 * 供应商必须在最近一次探测批次中可用；账本版本取当前最大版本，截止时间取
 * 服务端时钟。未知或已归档账户在此处明确拒绝，不落到外键错误。
 */
export async function createAgentSessionForRequest(input: {
  orchestrator: AgentTurnOrchestrator;
  writeCoordinator: WriteCoordinator;
  database: LyraDatabase;
  availability: AgentAvailabilitySnapshot | null;
  /** 发起创建请求且已通过访问令牌认证的设备。 */
  deviceId: string;
  request: CreateAgentSessionRequest;
  now: Date;
}): Promise<AgentSessionSnapshot> {
  const availability = input.availability?.providers.find(
    (candidate) => candidate.provider === input.request.provider,
  );
  if (availability?.status !== "available") {
    throw new ApplicationError({
      code: "AGENT_UNAVAILABLE",
      message: "目标 Agent 当前不可用",
      statusCode: 409,
      retryable: false,
    });
  }
  if (input.request.accountScope.kind === "account") {
    await assertActiveAccountExists(
      input.database,
      input.request.accountScope.account,
    );
  }
  const ledgerVersion = await readCurrentLedgerVersion(input.database);
  if (availability === undefined) {
    throw new ApplicationError({
      code: "AGENT_UNAVAILABLE",
      message: "目标 Agent 当前不可用",
      statusCode: 409,
      retryable: false,
    });
  }
  const session = createAgentSession({
    sessionId: randomUUID(),
    provider: input.request.provider,
    availability: { provider: input.request.provider, status: "available" },
    accountScope: input.request.accountScope,
    ledgerVersion,
    asOf: input.now.toISOString(),
  });
  await input.writeCoordinator.enqueue(() => persistNewAgentSession(input.database, {
    session,
    // 会话创建门禁此前必须已经通过设备认证；这里只需要设备仍处于 active。
    deviceId: input.deviceId,
    createdAt: input.now,
  }));
  await input.orchestrator.openSession(session.id);
  return session;
}

/**
 * 在 idle 回合边界显式关闭会话并持久化 closed 终态。
 *
 * 已关闭会话在 orchestrator 中保留快照，后续回合与重复关闭都被稳定拒绝。
 */
export async function closeAgentSessionForRequest(input: {
  orchestrator: AgentTurnOrchestrator;
  writeCoordinator: WriteCoordinator;
  database: LyraDatabase;
  sessionId: string;
  now: Date;
}): Promise<AgentSessionSnapshot> {
  const snapshot = await input.orchestrator.openSession(input.sessionId);
  if (snapshot.status !== "idle") {
    throw new ApplicationError({
      code: "AGENT_SESSION_STATE_CONFLICT",
      message: "Agent 会话当前不能关闭",
      statusCode: 409,
      retryable: false,
    });
  }
  const closed = transitionAgentSession(snapshot, "closed");
  await input.writeCoordinator.enqueue(() => persistAgentSessionClosed(input.database, {
    session: closed,
    closedAt: input.now.toISOString(),
  }));
  return closed;
}

/**
 * 在 idle 回合边界执行跨供应商切换并持久化切换边界。
 *
 * 目标供应商必须在最近一次探测批次中可用；切换后 provider、revision 与边界
 * system 消息在同一事务落库，orchestrator 采纳新快照并把 handoff 上下文留给
 * 切换后的首个回合一次性投递。
 */
export async function switchAgentSessionForRequest(input: {
  orchestrator: AgentTurnOrchestrator;
  writeCoordinator: WriteCoordinator;
  database: LyraDatabase;
  availability: AgentAvailabilitySnapshot;
  sessionId: string;
  targetProvider: AgentProvider;
  now: Date;
}): Promise<AgentSessionSnapshot> {
  const snapshot = await input.orchestrator.openSession(input.sessionId);
  const availability = input.availability.providers.find(
    (candidate) => candidate.provider === input.targetProvider,
  );
  if (availability === undefined) {
    throw new ApplicationError({
      code: "AGENT_UNAVAILABLE",
      message: "目标 Agent 当前不可用",
      statusCode: 409,
      retryable: false,
    });
  }
  const providerAvailability: AgentProviderAvailability = availability.status === "available"
    ? { provider: input.targetProvider, status: "available" }
    : { provider: input.targetProvider, status: "unavailable", reason: availability.reason };
  let plan: AgentSwitchPlan;
  try {
    plan = prepareAgentSwitch(
      snapshot,
      input.targetProvider,
      providerAvailability,
      randomUUID(),
      input.now.toISOString(),
    );
  } catch (error: unknown) {
    if (error instanceof AgentSwitchError) {
      throw new ApplicationError({
        code: error.code,
        message: error.message,
        statusCode: 409,
        retryable: false,
      });
    }
    if (error instanceof RangeError) {
      throw new ApplicationError({
        code: "INVALID_ARGUMENT",
        message: "Agent 切换目标无效",
        statusCode: 400,
        retryable: false,
      });
    }
    throw error;
  }
  await input.writeCoordinator.enqueue(() => persistAgentSessionSwitch(input.database, {
    session: plan.nextSession,
    previousProvider: snapshot.provider,
  }));
  input.orchestrator.adoptSwitchedSession(
    input.sessionId,
    plan.nextSession,
    formatHandoffContext(plan.handoff),
  );
  return plan.nextSession;
}

/** 把有界 handoff 上下文压缩为目标 CLI 可读的纯文本前缀。 */
function formatHandoffContext(handoff: AgentSwitchPlan["handoff"]): string {
  const lines: string[] = [
    `[Agent 切换] 本会话由 ${handoff.fromProvider} 切换到 ${handoff.toProvider}；`
      + `账户范围与账本版本 ${handoff.ledgerVersion}、数据截止 ${handoff.asOf} 保持不变。`,
  ];
  if (handoff.omittedMessageCount > 0) {
    lines.push(`较早的 ${handoff.omittedMessageCount} 条消息已被省略。`);
  }
  if (handoff.summary !== null) {
    lines.push(`会话摘要：${handoff.summary}`);
  }
  for (const message of handoff.messages) {
    lines.push(`[${message.role}] ${message.content}`);
  }
  return lines.join("\n");
}
