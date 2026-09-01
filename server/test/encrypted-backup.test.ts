import assert from "node:assert/strict";
import { access, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import {
  BackupIntegrityError,
  decryptBackupFile,
  encryptBackupFile,
  verifyEncryptedBackup,
} from "../src/infrastructure/backup/index.js";

describe("encrypted backup artifact", () => {
  it("流式加密、验证并解密为与源文件完全一致的候选文件", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-backup-test-"));
    try {
      const sourcePath = join(directory, "ledger.duckdb");
      const backupPath = join(directory, "ledger.lyrabak");
      const restoredPath = join(directory, "restored.duckdb");
      const source = Buffer.from("duckdb-checkpoint-content\n".repeat(8_192), "utf8");
      const key = Buffer.alloc(32, 7);
      await writeFile(sourcePath, source);

      const manifest = await encryptBackupFile({
        sourcePath,
        destinationPath: backupPath,
        key,
        schemaVersion: 6,
        now: new Date("2026-08-30T00:00:00Z"),
      });
      const encrypted = await readFile(backupPath);
      const verified = await verifyEncryptedBackup(backupPath, key);
      const restoredManifest = await decryptBackupFile({
        backupPath,
        destinationPath: restoredPath,
        key,
      });

      assert.equal(encrypted.includes(source.subarray(0, 64)), false);
      assert.equal(verified.sourceSha256, manifest.sourceSha256);
      assert.equal(restoredManifest.backupId, manifest.backupId);
      assert.deepEqual(await readFile(restoredPath), source);
    } finally {
      await rm(directory, { recursive: true });
    }
  });

  it("拒绝被篡改的密文且不发布解密目标", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-backup-tamper-"));
    try {
      const sourcePath = join(directory, "ledger.duckdb");
      const backupPath = join(directory, "ledger.lyrabak");
      const restoredPath = join(directory, "restored.duckdb");
      const key = Buffer.alloc(32, 11);
      await writeFile(sourcePath, Buffer.from("authoritative-ledger", "utf8"));
      await encryptBackupFile({
        sourcePath,
        destinationPath: backupPath,
        key,
        schemaVersion: 6,
        now: new Date("2026-08-30T00:00:00Z"),
      });

      const tampered = await readFile(backupPath);
      const tamperIndex = tampered.byteLength - 17;
      const originalByte = tampered[tamperIndex];
      if (originalByte === undefined) {
        throw new Error("测试备份没有可篡改的密文字节");
      }
      tampered[tamperIndex] = originalByte ^ 0x01;
      await writeFile(backupPath, tampered);

      await assert.rejects(verifyEncryptedBackup(backupPath, key), BackupIntegrityError);
      await assert.rejects(
        decryptBackupFile({ backupPath, destinationPath: restoredPath, key }),
        BackupIntegrityError,
      );
      await assert.rejects(access(restoredPath));
    } finally {
      await rm(directory, { recursive: true });
    }
  });
});
