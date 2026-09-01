import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  planBackupRetention,
  planScheduledBackup,
} from "../src/application/index.js";

describe("backup maintenance plan", () => {
  it("默认每日到期并阻止并发备份任务", () => {
    assert.deepEqual(planScheduledBackup({
      lastSuccessfulAt: "2026-08-30T01:00:00Z",
      now: new Date("2026-08-31T00:00:00Z"),
      backupInProgress: false,
    }), {
      kind: "skip",
      reason: "NOT_DUE",
      nextDueAt: "2026-08-31T01:00:00.000Z",
    });
    assert.deepEqual(planScheduledBackup({
      lastSuccessfulAt: "2026-08-30T01:00:00Z",
      now: new Date("2026-08-31T01:00:00Z"),
      backupInProgress: false,
    }), {
      kind: "start",
      dueAt: "2026-08-31T01:00:00.000Z",
    });
    assert.deepEqual(planScheduledBackup({
      lastSuccessfulAt: null,
      now: new Date("2026-08-31T01:00:00Z"),
      backupInProgress: true,
    }), {
      kind: "skip",
      reason: "BACKUP_IN_PROGRESS",
      nextDueAt: null,
    });
  });

  it("稳定保留最新已验证清单并只返回待删除 ID", () => {
    const manifests = [
      { backupId: "backup-old", createdAt: "2026-08-28T01:00:00Z" },
      { backupId: "backup-new-b", createdAt: "2026-08-31T01:00:00Z" },
      { backupId: "backup-middle", createdAt: "2026-08-30T01:00:00Z" },
      { backupId: "backup-new-a", createdAt: "2026-08-31T01:00:00Z" },
    ];
    assert.deepEqual(planBackupRetention(manifests, 2), {
      retainBackupIds: ["backup-new-a", "backup-new-b"],
      deleteBackupIds: ["backup-middle", "backup-old"],
    });
    assert.deepEqual(manifests.map((manifest) => manifest.backupId), [
      "backup-old", "backup-new-b", "backup-middle", "backup-new-a",
    ]);
  });
});
