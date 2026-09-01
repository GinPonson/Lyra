import type { AgentAvailabilitySnapshot } from "./agent-availability.js";
import type { PublicGmailConnection } from "./gmail-connection-view.js";
import type { WriteCoordinatorStatus } from "./write-coordinator.js";

/** 数据库探测只公开版本或不可用状态，不携带路径和底层错误。 */
export type DatabaseHealth =
  | { status: "available"; schemaVersion: number; ledgerVersion: number }
  | { status: "unavailable" };

/** App 服务信息页和监控端点共用的安全健康快照。 */
export interface ServiceHealthSnapshot {
  status: "healthy" | "degraded" | "unavailable";
  checkedAt: string;
  process: { status: "available" };
  database: DatabaseHealth;
  writeQueue: {
    status: "available" | "draining";
    pendingOperations: number;
  };
  gmail: {
    status: "available" | "degraded";
    totalConnections: number;
    attentionRequired: number;
  };
  agents: {
    status: "available" | "degraded" | "unknown";
    availableProviders: number;
    unavailableProviders: number;
  };
}

/**
 * 汇总不含个人信息和内部错误的服务健康状态。
 *
 * 数据库不可用或写协调器停止接收任务会令服务不可用。Gmail、Agent 等外部
 * 集成异常只令整体降级，账本只读能力仍可继续服务，且不会自动替换数据源或
 * Agent。
 */
export function createServiceHealthSnapshot(input: {
  database: DatabaseHealth;
  writeQueue: WriteCoordinatorStatus;
  gmailConnections: readonly Pick<PublicGmailConnection, "status">[];
  agentAvailability: AgentAvailabilitySnapshot | null;
  checkedAt: Date;
}): ServiceHealthSnapshot {
  if (
    input.database.status === "available"
    && (
      !Number.isSafeInteger(input.database.schemaVersion)
      || input.database.schemaVersion < 0
      || !Number.isSafeInteger(input.database.ledgerVersion)
      || input.database.ledgerVersion < 0
    )
  ) {
    throw new RangeError("数据库健康版本必须是非负安全整数");
  }
  if (
    !Number.isSafeInteger(input.writeQueue.pendingOperations)
    || input.writeQueue.pendingOperations < 0
  ) {
    throw new RangeError("写队列待处理数量必须是非负安全整数");
  }
  if (Number.isNaN(input.checkedAt.getTime())) {
    throw new RangeError("服务健康检查时间无效");
  }

  const attentionRequired = input.gmailConnections.filter((connection) => (
    connection.status === "needs_resync" || connection.status === "error"
  )).length;
  const availableProviders = input.agentAvailability?.providers.filter((provider) => (
    provider.status === "available"
  )).length ?? 0;
  const unavailableProviders = input.agentAvailability?.providers.length === undefined
    ? 0
    : input.agentAvailability.providers.length - availableProviders;
  const agentStatus = input.agentAvailability === null
    ? "unknown" as const
    : unavailableProviders > 0
      ? "degraded" as const
      : "available" as const;
  const serviceUnavailable = input.database.status === "unavailable"
    || !input.writeQueue.accepting;
  const externalDegraded = attentionRequired > 0 || agentStatus !== "available";

  return {
    status: serviceUnavailable
      ? "unavailable"
      : externalDegraded
        ? "degraded"
        : "healthy",
    checkedAt: input.checkedAt.toISOString(),
    process: { status: "available" },
    database: input.database.status === "available"
      ? {
          status: "available",
          schemaVersion: input.database.schemaVersion,
          ledgerVersion: input.database.ledgerVersion,
        }
      : { status: "unavailable" },
    writeQueue: {
      status: input.writeQueue.accepting ? "available" : "draining",
      pendingOperations: input.writeQueue.pendingOperations,
    },
    gmail: {
      status: attentionRequired > 0 ? "degraded" : "available",
      totalConnections: input.gmailConnections.length,
      attentionRequired,
    },
    agents: {
      status: agentStatus,
      availableProviders,
      unavailableProviders,
    },
  };
}
