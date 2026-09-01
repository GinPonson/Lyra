import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { Readable, Writable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { describe, it } from "node:test";
import {
  ApplicationError,
  ImportUploadMonitor,
} from "../src/application/index.js";

describe("import upload stream monitor", () => {
  it("原样透传分块内容并生成有界样本、大小与 SHA-256", async () => {
    const chunks = [Buffer.from("Date,Action\n"), Buffer.from("08/30/2026,Buy\n")];
    const received: Buffer[] = [];
    const monitor = new ImportUploadMonitor({
      fileName: "Individual_Transactions_20260830.csv",
      maxSizeBytes: 1024,
      sampleSizeBytes: 8,
    });
    await pipeline(
      Readable.from(chunks),
      monitor,
      new Writable({
        write(chunk: Buffer, _encoding, callback) {
          received.push(Buffer.from(chunk));
          callback();
        },
      }),
    );

    const content = Buffer.concat(chunks);
    const summary = monitor.summary();
    assert.deepEqual(Buffer.concat(received), content);
    assert.equal(summary.fileType, "csv");
    assert.equal(summary.sizeBytes, content.byteLength);
    assert.equal(summary.sha256, createHash("sha256").update(content).digest("hex"));
    assert.deepEqual(Buffer.from(summary.leadingBytes), content.subarray(0, 8));
    assert.equal(monitor.summary(), summary);
  });

  it("达到硬上限后拒绝当前分块且返回稳定导入错误", async () => {
    const received: Buffer[] = [];
    const monitor = new ImportUploadMonitor({
      fileName: "transactions.csv",
      maxSizeBytes: 5,
    });

    await assert.rejects(
      pipeline(
        Readable.from([Buffer.from("1234"), Buffer.from("56")]),
        monitor,
        new Writable({
          write(chunk: Buffer, _encoding, callback) {
            received.push(Buffer.from(chunk));
            callback();
          },
        }),
      ),
      (error: unknown) => {
        assert.ok(error instanceof ApplicationError);
        assert.equal(error.code, "IMPORT_VALIDATION_FAILED");
        assert.equal(error.statusCode, 422);
        return true;
      },
    );
    assert.deepEqual(Buffer.concat(received), Buffer.from("1234"));
    assert.throws(() => monitor.summary(), /尚未成功结束/);
  });
});
