import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createPublicBackupStatus } from "../src/application/index.js";
import type { EncryptedBackupManifest } from "../src/infrastructure/backup/index.js";

const MANIFESTS: readonly EncryptedBackupManifest[] = [
  {
    format: "lyra-encrypted-backup",
    formatVersion: 1,
    algorithm: "aes-256-gcm",
    backupId: "backup-old",
    createdAt: "2026-08-30T01:00:00.000Z",
    schemaVersion: 9,
    iv: "SENSITIVE IV OLD",
    sourceBytes: 2_048,
    sourceSha256: "a".repeat(64),
  },
  {
    format: "lyra-encrypted-backup",
    formatVersion: 1,
    algorithm: "aes-256-gcm",
    backupId: "backup-new",
    createdAt: "2026-08-31T01:00:00.000Z",
    schemaVersion: 9,
    iv: "SENSITIVE IV NEW",
    sourceBytes: 4_096,
    sourceSha256: "b".repeat(64),
  },
];

describe("public backup status", () => {
  it("稳定公开备份状态且隔离路径相关完整性字段", () => {
    const result = createPublicBackupStatus({
      manifests: MANIFESTS,
      schedule: {
        kind: "skip",
        reason: "NOT_DUE",
        nextDueAt: "2026-09-01T01:00:00.000Z",
      },
      retentionCount: 7,
    });

    assert.deepEqual(result, {
      status: "scheduled",
      nextDueAt: "2026-09-01T01:00:00.000Z",
      retentionCount: 7,
      backups: [
        {
          id: "backup-new",
          createdAt: "2026-08-31T01:00:00.000Z",
          schemaVersion: 9,
          sourceBytes: 4_096,
        },
        {
          id: "backup-old",
          createdAt: "2026-08-30T01:00:00.000Z",
          schemaVersion: 9,
          sourceBytes: 2_048,
        },
      ],
    });
    const serialized = JSON.stringify(result);
    for (const forbidden of ["iv", "sourceSha256", "algorithm", "SENSITIVE IV"]) {
      assert.equal(serialized.includes(forbidden), false);
    }
    assert.deepEqual(MANIFESTS.map((manifest) => manifest.backupId), [
      "backup-old", "backup-new",
    ]);
  });
});
