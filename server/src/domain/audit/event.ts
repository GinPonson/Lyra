import { randomUUID } from "node:crypto";

/** 方案中需要形成稳定审计语义的事件类型。 */
export const AUDIT_EVENT_TYPES = [
  "device.paired",
  "device.refreshed",
  "device.revoked",
  "auth.failed",
  "account.created",
  "account.renamed",
  "account.archived",
  "import.uploaded",
  "import.previewed",
  "import.committed",
  "import.rejected",
  "import.deleted",
  "gmail.authorized",
  "gmail.synced",
  "gmail.resync_requested",
  "gmail.mapped",
  "gmail.disconnected",
  "gmail.revoked",
  "ledger.rebuilt",
  "agent.session_started",
  "agent.switched",
  "agent.tool_called",
  "agent.tool_rejected",
  "agent.failed",
  "backup.created",
  "backup.restored",
  "integrity.checked",
] as const;

/** 编译期与运行时共享的审计事件名。 */
export type AuditEventType = typeof AUDIT_EVENT_TYPES[number];

/** 审计元数据保持扁平，避免把邮件正文、模型上下文或复杂业务对象写入日志。 */
export type AuditMetadata = Readonly<Record<string, string | number | boolean | null>>;

/** 审计策略拒绝了不安全或不一致的事件。 */
export class AuditEventError extends Error {
  /** @param message - 可安全写入服务端日志的拒绝原因。 */
  constructor(message: string) {
    super(message);
    this.name = "AuditEventError";
  }
}

/** 建立一条审计事件所需的数据。 */
export interface CreateAuditEventInput {
  eventType: AuditEventType;
  outcome: "success" | "denied" | "failure";
  actorType: "system" | "device";
  actorId?: string | null;
  requestId?: string | null;
  targetType?: string | null;
  targetId?: string | null;
  accountId?: string | null;
  ledgerVersion?: number | null;
  errorCode?: string | null;
  metadata?: AuditMetadata;
  now: Date;
}

/** 可直接交给持久化适配器的规范审计事件。 */
export interface AuditEvent {
  id: string;
  occurredAt: string;
  eventType: AuditEventType;
  outcome: "success" | "denied" | "failure";
  actorType: "system" | "device";
  actorId: string | null;
  requestId: string | null;
  targetType: string | null;
  targetId: string | null;
  accountId: string | null;
  ledgerVersion: number | null;
  errorCode: string | null;
  metadata: AuditMetadata;
}

/** 明确禁止进入审计 metadata 的敏感字段名。 */
const FORBIDDEN_METADATA_KEYS = new Set([
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
  "pairingcode",
]);

/**
 * 验证并建立一条最小化审计事件。
 *
 * @param input - 事件语义、主体、目标和扁平安全元数据。
 * @returns 字段规范化且带 UUID/时间戳的不可变事件。
 * @throws AuditEventError 当主体、结果或 metadata 违反安全策略时抛出。
 */
export function createAuditEvent(input: CreateAuditEventInput): AuditEvent {
  const actorId = input.actorId ?? null;
  if (input.actorType === "device" && actorId === null) {
    throw new AuditEventError("设备审计事件必须包含 actorId");
  }
  if (input.actorType === "system" && actorId !== null) {
    throw new AuditEventError("系统审计事件不能伪装成设备主体");
  }

  const errorCode = input.errorCode ?? null;
  if (input.outcome === "success" && errorCode !== null) {
    throw new AuditEventError("成功审计事件不能包含错误码");
  }
  if (input.outcome !== "success" && errorCode === null) {
    throw new AuditEventError("拒绝或失败审计事件必须包含稳定错误码");
  }

  const targetType = input.targetType ?? null;
  const targetId = input.targetId ?? null;
  if ((targetType === null) !== (targetId === null)) {
    throw new AuditEventError("审计目标类型和目标 ID 必须同时提供");
  }

  const metadata = input.metadata ?? {};
  for (const [key, value] of Object.entries(metadata)) {
    const normalizedKey = key.toLowerCase().replace(/[^a-z0-9]/g, "");
    if (FORBIDDEN_METADATA_KEYS.has(normalizedKey)) {
      throw new AuditEventError(`审计元数据禁止包含敏感字段: ${key}`);
    }
    if (typeof value === "number" && !Number.isFinite(value)) {
      throw new AuditEventError(`审计元数据必须使用有限数值: ${key}`);
    }
  }

  return {
    id: randomUUID(),
    occurredAt: input.now.toISOString(),
    eventType: input.eventType,
    outcome: input.outcome,
    actorType: input.actorType,
    actorId,
    requestId: input.requestId ?? null,
    targetType,
    targetId,
    accountId: input.accountId ?? null,
    ledgerVersion: input.ledgerVersion ?? null,
    errorCode,
    metadata,
  };
}
