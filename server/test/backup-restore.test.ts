import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { planBackupRestore } from "../src/application/index.js";

describe("backup restore admission plan", () => {
  it("允许兼容备份进入隔离恢复并明确迁移、校验和 Gmail 重授权", () => {
    const manifest = { backupId: " backup-20260831 ", schemaVersion: 7 };

    assert.deepEqual(planBackupRestore({
      manifest,
      currentSchemaVersion: 9,
      minimumRestorableSchemaVersion: 1,
      gmailCredentialsIncluded: false,
      restoreInProgress: false,
    }), {
      kind: "start",
      backupId: "backup-20260831",
      sourceSchemaVersion: 7,
      targetSchemaVersion: 9,
      migrationsRequired: true,
      gmailAuthorization: "reauthorize",
      requiredChecks: ["schema", "ledger_hash", "fifo"],
      replacementRequiresConfirmation: true,
    });
    assert.deepEqual(manifest, {
      backupId: " backup-20260831 ",
      schemaVersion: 7,
    });
  });

  it("明确拒绝并发恢复以及迁移链以外的 schema 版本", () => {
    const baseInput = {
      currentSchemaVersion: 9,
      minimumRestorableSchemaVersion: 1,
      gmailCredentialsIncluded: true,
    } as const;

    assert.deepEqual(planBackupRestore({
      ...baseInput,
      manifest: { backupId: "backup-running", schemaVersion: 9 },
      restoreInProgress: true,
    }), {
      kind: "reject",
      backupId: "backup-running",
      reason: "RESTORE_IN_PROGRESS",
    });
    assert.deepEqual(planBackupRestore({
      ...baseInput,
      manifest: { backupId: "backup-old", schemaVersion: 0 },
      restoreInProgress: false,
    }), {
      kind: "reject",
      backupId: "backup-old",
      reason: "SCHEMA_TOO_OLD",
    });
    assert.deepEqual(planBackupRestore({
      ...baseInput,
      manifest: { backupId: "backup-future", schemaVersion: 10 },
      restoreInProgress: false,
    }), {
      kind: "reject",
      backupId: "backup-future",
      reason: "SCHEMA_NEWER_THAN_SERVER",
    });
  });
});
