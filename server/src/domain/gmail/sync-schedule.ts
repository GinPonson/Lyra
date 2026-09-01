import {
  normalizeGmailSyncTimestamp,
  planGmailSync,
} from "./sync-state.js";
import type {
  GmailSyncRequest,
  GmailSyncState,
} from "./sync-state.js";

/** Gmail 自动增量同步的默认周期：60 分钟。 */
export const DEFAULT_GMAIL_SYNC_INTERVAL_MINUTES = 60;

/** 定时器在当前时刻应执行或跳过的确定性决策。 */
export type GmailScheduledSyncPlan =
  | {
      kind: "start";
      dueAt: string;
      request: GmailSyncRequest;
    }
  | {
      kind: "skip";
      reason: "CONNECTION_NOT_ACTIVE" | "SYNC_IN_PROGRESS" | "NOT_DUE";
      nextDueAt: string | null;
    };

/**
 * 为单个 Gmail 连接生成下一次自动同步决策。
 *
 * 定时任务只处理 active 增量连接；首次扫描和 history 失效后的重扫仍必须由
 * 用户确认范围。手动同步直接调用 `planGmailSync`，不受本函数的到期判断限制。
 */
export function planScheduledGmailSync(input: {
  state: GmailSyncState;
  now: Date;
  syncInProgress: boolean;
  intervalMinutes?: number;
}): GmailScheduledSyncPlan {
  const intervalMinutes = input.intervalMinutes ?? DEFAULT_GMAIL_SYNC_INTERVAL_MINUTES;
  if (!Number.isSafeInteger(intervalMinutes) || intervalMinutes <= 0) {
    throw new RangeError("Gmail 自动同步周期必须是正安全整数分钟");
  }
  if (Number.isNaN(input.now.getTime())) {
    throw new RangeError("Gmail 自动同步当前时间无效");
  }
  if (input.state.status !== "active") {
    return { kind: "skip", reason: "CONNECTION_NOT_ACTIVE", nextDueAt: null };
  }
  if (input.syncInProgress) {
    return { kind: "skip", reason: "SYNC_IN_PROGRESS", nextDueAt: null };
  }

  const dueAt = input.state.lastSyncAt === null
    ? input.now.toISOString()
    : new Date(
      new Date(normalizeGmailSyncTimestamp(input.state.lastSyncAt)).getTime()
      + intervalMinutes * 60_000,
    ).toISOString();
  if (input.now.getTime() < new Date(dueAt).getTime()) {
    return { kind: "skip", reason: "NOT_DUE", nextDueAt: dueAt };
  }
  return {
    kind: "start",
    dueAt,
    request: planGmailSync(input.state),
  };
}
