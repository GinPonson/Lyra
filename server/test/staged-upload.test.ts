import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, readdir, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Readable } from "node:stream";
import { describe, it } from "node:test";
import { ApplicationError } from "../src/application/index.js";
import { stageImportUpload } from "../src/infrastructure/imports/staged-upload.js";

describe("staged import upload", () => {
  it("以私有随机路径暂存上传并返回可复核摘要", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-import-stage-"));
    try {
      const content = Buffer.from("Date,Action\n08/30/2026,Buy\n", "utf8");
      const staged = await stageImportUpload({
        stream: Readable.from([content.subarray(0, 7), content.subarray(7)]),
        stagingDirectory: directory,
        fileName: "Individual_Transactions.csv",
        maxSizeBytes: 1024,
      });

      assert.equal(staged.filePath.startsWith(`${directory}/.lyra-import-`), true);
      assert.deepEqual(await readFile(staged.filePath), content);
      assert.equal((await stat(staged.filePath)).mode & 0o777, 0o600);
      assert.equal(staged.summary.fileName, "Individual_Transactions.csv");
      assert.equal(
        staged.summary.sha256,
        createHash("sha256").update(content).digest("hex"),
      );
      await staged.dispose();
      await staged.dispose();
      assert.deepEqual(await readdir(directory), []);
    } finally {
      await rm(directory, { recursive: true });
    }
  });

  it("上传超限时删除已写入的部分临时文件", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-import-limit-"));
    try {
      await assert.rejects(
        stageImportUpload({
          stream: Readable.from([Buffer.from("1234"), Buffer.from("56")]),
          stagingDirectory: directory,
          fileName: "transactions.csv",
          maxSizeBytes: 5,
        }),
        (error: unknown) => {
          assert.ok(error instanceof ApplicationError);
          assert.equal(error.code, "IMPORT_VALIDATION_FAILED");
          return true;
        },
      );
      assert.deepEqual(await readdir(directory), []);
    } finally {
      await rm(directory, { recursive: true });
    }
  });
});
