import {
  completeGmailSync,
  GmailSyncError,
} from "./sync-state.js";
import type { GmailSyncState } from "./sync-state.js";

/** 已经持久化、因此不会阻止 history 游标推进的单消息结果。 */
export type DurableGmailMessageOutcomeStatus =
  | "imported"
  | "already_imported"
  | "rejected"
  | "conflict";

/** Gmail 获取、解析或写事务尚未成功时的非持久完成结果。 */
export type GmailSyncMessageOutcome =
  | {
      messageId: string;
      status: DurableGmailMessageOutcomeStatus;
    }
  | {
      messageId: string;
      status: "failed";
      errorCode: string;
    };

/** 同步批次完成后可用于审计和 App 状态展示的安全摘要。 */
export interface CompletedGmailSyncBatch {
  state: GmailSyncState;
  total: number;
  imported: number;
  alreadyImported: number;
  rejected: number;
  conflict: number;
}

/**
 * 仅在本批全部唯一消息都已有持久结果后推进 Gmail history 游标。
 *
 * `rejected` 和 `conflict` 代表相应元数据已经写入，后续可人工处理，所以不会
 * 让同步永久卡在同一游标；`failed` 或缺少结果表示获取/解析/事务未完成，必须
 * 保留旧游标重试。调用方应先完成消息写事务，再调用本函数持久化连接新状态。
 *
 * @param input - 起始连接状态、候选 message ID、持久结果和 Gmail 最新游标。
 * @returns 新连接状态及不含正文和错误详情的批次计数。
 */
export function completeGmailSyncBatch(input: {
  state: GmailSyncState;
  expectedMessageIds: readonly string[];
  outcomes: readonly GmailSyncMessageOutcome[];
  newHistoryId: string;
  syncedAt: string;
}): CompletedGmailSyncBatch {
  const expectedIds = new Set(input.expectedMessageIds.map((id) => requireText(id)));
  const outcomeById = new Map<string, GmailSyncMessageOutcome>();
  for (const outcome of input.outcomes) {
    const messageId = requireText(outcome.messageId);
    if (!expectedIds.has(messageId)) {
      throw new RangeError("Gmail 同步结果包含批次之外的 message ID");
    }
    if (outcomeById.has(messageId)) {
      throw new RangeError("Gmail 同步结果包含重复 message ID");
    }
    outcomeById.set(messageId, { ...outcome, messageId });
  }

  for (const messageId of expectedIds) {
    const outcome = outcomeById.get(messageId);
    if (outcome === undefined || outcome.status === "failed") {
      throw new GmailSyncError(
        "GMAIL_CONNECTION_ERROR",
        "Gmail 同步批次尚未全部完成，history 游标保持不变",
      );
    }
  }

  const outcomes = [...outcomeById.values()];
  return {
    state: completeGmailSync(input.state, input.newHistoryId, input.syncedAt),
    total: expectedIds.size,
    imported: countStatus(outcomes, "imported"),
    alreadyImported: countStatus(outcomes, "already_imported"),
    rejected: countStatus(outcomes, "rejected"),
    conflict: countStatus(outcomes, "conflict"),
  };
}

/** 对持久结果进行固定枚举计数。 */
function countStatus(
  outcomes: readonly GmailSyncMessageOutcome[],
  status: DurableGmailMessageOutcomeStatus,
): number {
  return outcomes.filter((outcome) => outcome.status === status).length;
}

/** 规范 message ID 并拒绝空白值。 */
function requireText(value: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError("Gmail message ID 不能为空");
  }
  return normalized;
}
