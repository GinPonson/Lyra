import type { EncryptedBackupManifest } from "../infrastructure/backup/index.js";

/** 默认每日创建一次经过认证的加密备份。 */
export const DEFAULT_BACKUP_INTERVAL_HOURS = 24;

/** 定时器在当前时刻应启动或跳过备份的确定性决策。 */
export type ScheduledBackupPlan =
  | { kind: "start"; dueAt: string }
  | {
      kind: "skip";
      reason: "BACKUP_IN_PROGRESS" | "NOT_DUE";
      nextDueAt: string | null;
    };

/** 备份轮换只返回精确清单 ID，由仓储在验证路径归属后执行。 */
export interface BackupRetentionPlan {
  retainBackupIds: readonly string[];
  deleteBackupIds: readonly string[];
}

/**
 * 根据最近一次成功备份生成定时执行决策。
 *
 * @param input.lastSuccessfulAt - 最近成功且通过认证的备份时间；从未成功时为 null。
 * @param input.now - 服务端可信当前时间。
 * @param input.backupInProgress - 当前是否已有 checkpoint/加密任务运行。
 * @param input.intervalHours - 正整数小时周期，默认 24 小时。
 */
export function planScheduledBackup(input: {
  lastSuccessfulAt: string | null;
  now: Date;
  backupInProgress: boolean;
  intervalHours?: number;
}): ScheduledBackupPlan {
  const intervalHours = input.intervalHours ?? DEFAULT_BACKUP_INTERVAL_HOURS;
  if (!Number.isSafeInteger(intervalHours) || intervalHours <= 0) {
    throw new RangeError("自动备份周期必须是正安全整数小时");
  }
  if (Number.isNaN(input.now.getTime())) {
    throw new RangeError("自动备份当前时间无效");
  }
  if (input.backupInProgress) {
    return { kind: "skip", reason: "BACKUP_IN_PROGRESS", nextDueAt: null };
  }

  const dueAt = input.lastSuccessfulAt === null
    ? input.now.toISOString()
    : new Date(
      requireTimestamp(input.lastSuccessfulAt, "最近成功备份时间")
      + intervalHours * 60 * 60 * 1000,
    ).toISOString();
  if (input.now.getTime() < Date.parse(dueAt)) {
    return { kind: "skip", reason: "NOT_DUE", nextDueAt: dueAt };
  }
  return { kind: "start", dueAt };
}

/**
 * 为已经验证的加密备份清单生成保留与删除 ID。
 *
 * 保留数量是显式配置，不在此处猜测存储容量。相同时间使用 backupId 保证稳定
 * 顺序；本函数不接收或删除路径，防止轮换计划扩大为不受控文件操作。
 */
export function planBackupRetention(
  manifests: readonly Pick<EncryptedBackupManifest, "backupId" | "createdAt">[],
  retentionCount: number,
): BackupRetentionPlan {
  if (!Number.isSafeInteger(retentionCount) || retentionCount <= 0) {
    throw new RangeError("加密备份保留数量必须是正安全整数");
  }
  const seenIds = new Set<string>();
  for (const manifest of manifests) {
    if (manifest.backupId.trim().length === 0 || seenIds.has(manifest.backupId)) {
      throw new RangeError("加密备份清单 ID 不能为空或重复");
    }
    requireTimestamp(manifest.createdAt, "加密备份创建时间");
    seenIds.add(manifest.backupId);
  }
  const ordered = manifests.toSorted((left, right) => (
    Date.parse(right.createdAt) - Date.parse(left.createdAt)
    || left.backupId.localeCompare(right.backupId)
  ));
  return {
    retainBackupIds: ordered.slice(0, retentionCount).map((manifest) => manifest.backupId),
    deleteBackupIds: ordered.slice(retentionCount).map((manifest) => manifest.backupId),
  };
}

/** 严格解析带时区时间戳，避免把本地时间误用于备份调度。 */
function requireTimestamp(value: string, label: string): number {
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    throw new RangeError(`${label}必须包含时区`);
  }
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) {
    throw new RangeError(`${label}无效`);
  }
  return timestamp;
}
