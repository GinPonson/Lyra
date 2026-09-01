import type { EncryptedBackupManifest } from "../infrastructure/backup/index.js";

/** 隔离恢复候选库在替换运行数据库前必须完成的检查。 */
const REQUIRED_RESTORE_CHECKS = Object.freeze([
  "schema",
  "ledger_hash",
  "fifo",
] as const);

/** 已验证备份进入隔离恢复流程的确定性决策。 */
export type BackupRestorePlan =
  | {
      kind: "start";
      backupId: string;
      sourceSchemaVersion: number;
      targetSchemaVersion: number;
      migrationsRequired: boolean;
      gmailAuthorization: "verify_existing" | "reauthorize";
      requiredChecks: readonly (typeof REQUIRED_RESTORE_CHECKS)[number][];
      replacementRequiresConfirmation: true;
    }
  | {
      kind: "reject";
      backupId: string;
      reason:
        | "RESTORE_IN_PROGRESS"
        | "SCHEMA_TOO_OLD"
        | "SCHEMA_NEWER_THAN_SERVER";
    };

/**
 * 为已通过认证和明文完整性检查的备份生成隔离恢复准入计划。
 *
 * 本函数不接收文件路径，也不替换运行中的 DuckDB。调用方须先解密到隔离位置，
 * 执行返回的强制检查，再经过人工确认进入原子替换步骤。
 *
 * @param input.manifest - 已由备份基础设施完整验证的加密清单。
 * @param input.currentSchemaVersion - 当前服务端支持的最新 schema 版本。
 * @param input.minimumRestorableSchemaVersion - 当前迁移链可接受的最旧版本。
 * @param input.gmailCredentialsIncluded - 备份策略是否包含加密后的 Gmail 凭据。
 * @param input.restoreInProgress - 当前是否已有隔离恢复任务运行。
 */
export function planBackupRestore(input: {
  manifest: Pick<EncryptedBackupManifest, "backupId" | "schemaVersion">;
  currentSchemaVersion: number;
  minimumRestorableSchemaVersion: number;
  gmailCredentialsIncluded: boolean;
  restoreInProgress: boolean;
}): BackupRestorePlan {
  const backupId = input.manifest.backupId.trim();
  if (backupId.length === 0) {
    throw new RangeError("恢复备份 ID 不能为空");
  }
  assertSchemaVersion(input.manifest.schemaVersion, "恢复备份 schema 版本");
  assertSchemaVersion(input.currentSchemaVersion, "当前 schema 版本");
  assertSchemaVersion(
    input.minimumRestorableSchemaVersion,
    "最低可恢复 schema 版本",
  );
  if (input.minimumRestorableSchemaVersion > input.currentSchemaVersion) {
    throw new RangeError("最低可恢复 schema 版本不能高于当前版本");
  }

  if (input.restoreInProgress) {
    return { kind: "reject", backupId, reason: "RESTORE_IN_PROGRESS" };
  }
  if (input.manifest.schemaVersion < input.minimumRestorableSchemaVersion) {
    return { kind: "reject", backupId, reason: "SCHEMA_TOO_OLD" };
  }
  if (input.manifest.schemaVersion > input.currentSchemaVersion) {
    return { kind: "reject", backupId, reason: "SCHEMA_NEWER_THAN_SERVER" };
  }

  return {
    kind: "start",
    backupId,
    sourceSchemaVersion: input.manifest.schemaVersion,
    targetSchemaVersion: input.currentSchemaVersion,
    migrationsRequired: input.manifest.schemaVersion < input.currentSchemaVersion,
    gmailAuthorization: input.gmailCredentialsIncluded
      ? "verify_existing"
      : "reauthorize",
    requiredChecks: REQUIRED_RESTORE_CHECKS,
    replacementRequiresConfirmation: true,
  };
}

/** 验证 schema 版本可安全用于迁移边界比较。 */
function assertSchemaVersion(value: number, label: string): void {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new RangeError(`${label}必须是非负安全整数`);
  }
}
