import type { EncryptedBackupManifest } from "../infrastructure/backup/index.js";
import type { ScheduledBackupPlan } from "./backup-maintenance.js";

/** 账户中心可展示的单个已验证加密备份。 */
export interface PublicEncryptedBackup {
  id: string;
  createdAt: string;
  schemaVersion: number;
  sourceBytes: number;
}

/** 账户中心的备份维护状态和安全清单。 */
export interface PublicBackupStatus {
  status: "scheduled" | "due" | "running";
  nextDueAt: string | null;
  retentionCount: number;
  backups: readonly PublicEncryptedBackup[];
}

/**
 * 创建不含路径和完整性秘密的备份状态。
 *
 * 调用方只应传入 `verifyEncryptedBackup` 成功后的清单。IV、算法和明文 SHA-256
 * 继续留在服务端认证信封中；列表按最新时间和 backup ID 稳定排序。
 */
export function createPublicBackupStatus(input: {
  manifests: readonly EncryptedBackupManifest[];
  schedule: ScheduledBackupPlan;
  retentionCount: number;
}): PublicBackupStatus {
  if (!Number.isSafeInteger(input.retentionCount) || input.retentionCount <= 0) {
    throw new RangeError("公开备份保留数量必须是正安全整数");
  }
  return {
    status: input.schedule.kind === "start"
      ? "due"
      : input.schedule.reason === "BACKUP_IN_PROGRESS"
        ? "running"
        : "scheduled",
    nextDueAt: input.schedule.kind === "start"
      ? input.schedule.dueAt
      : input.schedule.nextDueAt,
    retentionCount: input.retentionCount,
    backups: input.manifests
      .toSorted((left, right) => (
        Date.parse(right.createdAt) - Date.parse(left.createdAt)
        || left.backupId.localeCompare(right.backupId)
      ))
      .map((manifest) => ({
        id: manifest.backupId,
        createdAt: manifest.createdAt,
        schemaVersion: manifest.schemaVersion,
        sourceBytes: manifest.sourceBytes,
      })),
  };
}
