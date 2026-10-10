import { randomUUID } from "node:crypto";

import type {
  AgentProvider,
  AgentSessionSnapshot,
} from "../domain/agent/index.js";
import {
  createAgentSession,
  transitionAgentSession,
} from "../domain/agent/index.js";
import type { AgentAvailabilitySnapshot } from "./agent-availability.js";
import type { AgentTurnOrchestrator } from "./agent-turn-orchestrator.js";
import { ApplicationError } from "./app-error.js";
import type { WriteCoordinator } from "./write-coordinator.js";
import {
  assertActiveAccountExists,
  persistAgentSessionClosed,
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
 * Genkit 后端必须已配置；账本版本取当前最大版本，截止时间取
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
  input.orchestrator.adoptSession(closed);
  return closed;
}
