import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { ApplicationError, inspectImportUpload } from "../src/application/index.js";

describe("import upload preflight", () => {
  it("接受文本 CSV 与具有 ZIP 签名的 XLSX", () => {
    const csv = inspectImportUpload({
      fileName: "Individual_TST001_Transactions.CSV",
      sizeBytes: 128,
      leadingBytes: Buffer.from("\uFEFFDate,Action", "utf8"),
      maxSizeBytes: 1_024,
    });
    const xlsx = inspectImportUpload({
      fileName: "transactions.xlsx",
      sizeBytes: 256,
      leadingBytes: Buffer.from([0x50, 0x4b, 0x03, 0x04, 0x14, 0x00]),
      maxSizeBytes: 1_024,
    });

    assert.equal(csv.fileType, "csv");
    assert.equal(xlsx.fileType, "xlsx");
  });

  it("用稳定错误码拒绝不支持格式与伪装扩展名", () => {
    assert.throws(
      () => inspectImportUpload({
        fileName: "transactions.xls",
        sizeBytes: 128,
        leadingBytes: Buffer.from([0xd0, 0xcf, 0x11, 0xe0]),
        maxSizeBytes: 1_024,
      }),
      (error: unknown) => (
        error instanceof ApplicationError && error.code === "IMPORT_UNSUPPORTED_FORMAT"
      ),
    );
    assert.throws(
      () => inspectImportUpload({
        fileName: "transactions.xlsx",
        sizeBytes: 128,
        leadingBytes: Buffer.from("Date,Action", "utf8"),
        maxSizeBytes: 1_024,
      }),
      (error: unknown) => (
        error instanceof ApplicationError && error.code === "IMPORT_VALIDATION_FAILED"
      ),
    );
  });

  it("拒绝路径式文件名、超限文件与含 NUL 的 CSV 样本", () => {
    const cases = [
      { fileName: "../transactions.csv", sizeBytes: 10, leadingBytes: Buffer.from("Date") },
      { fileName: "transactions.csv", sizeBytes: 2_048, leadingBytes: Buffer.from("Date") },
      { fileName: "transactions.csv", sizeBytes: 10, leadingBytes: Buffer.from([0x44, 0x00]) },
    ];
    for (const input of cases) {
      assert.throws(
        () => inspectImportUpload({ ...input, maxSizeBytes: 1_024 }),
        (error: unknown) => (
          error instanceof ApplicationError && error.code === "IMPORT_VALIDATION_FAILED"
        ),
      );
    }
  });
});
