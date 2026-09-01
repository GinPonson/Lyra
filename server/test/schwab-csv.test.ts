import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ApplicationError,
  createImportPreview,
} from "../src/application/index.js";
import { parseSchwabCsv } from "../src/infrastructure/imports/schwab-csv.js";

const FILE_NAME = "Individual_TST001_Transactions_20260831-080000.csv";

describe("Schwab CSV adapter", () => {
  it("解析 BOM、逗号与转义引号，并复用统一预览内核", () => {
    const content = Buffer.from([
      "\uFEFF\"Date\",\"Action\",\"Symbol\",\"Description\",\"Quantity\",\"Price\",\"Fees & Comm\",\"Amount\"",
      "\"08/30/2026\",\"Buy\",\"LYRA\",\"LYRA, INC \"\"CLASS A\"\"\",\"2\",\"$10.00\",\"$0.01\",\"-$20.01\"",
      "\"08/31/2026\",\"Qualified Dividend\",\"LYRA\",\"LYRA, INC\",\"\",\"\",\"\",\"$1.25\"",
      "",
    ].join("\r\n"), "utf8");
    const batch = parseSchwabCsv({
      content,
      fileName: FILE_NAME,
      sourceHash: "source-hash-1",
      sourceReference: "import-1",
    });
    const preview = createImportPreview({
      batch,
      parserVersion: "schwab-csv-v1",
      baseLedgerVersion: 7,
      existingTransactionHashes: new Set<string>(),
    });

    assert.equal(batch.account, "TST001");
    assert.equal(batch.sourceType, "csv");
    assert.equal(batch.rows[0]?.Description, "LYRA, INC \"CLASS A\"");
    assert.equal(preview.totalRows, 2);
    assert.equal(preview.insertableRows, 2);
    assert.deepEqual(preview.byAction, { Buy: 1, "Qualified Dividend": 1 });
    assert.equal(preview.transactions[0]?.sourceReference, "import-1");
  });

  it("明确拒绝列数不一致、未闭合引号与非法 UTF-8", () => {
    const header = "\"Date\",\"Action\",\"Symbol\",\"Description\",\"Quantity\",\"Price\",\"Fees & Comm\",\"Amount\"";
    const invalidInputs: readonly Uint8Array[] = [
      Buffer.from(`${header}\n\"08/30/2026\",\"Buy\"`, "utf8"),
      Buffer.from(`${header}\n\"08/30/2026,\"Buy\",\"LYRA\"`, "utf8"),
      Uint8Array.from([0xff, 0xfe, 0xfd]),
    ];

    for (const content of invalidInputs) {
      assert.throws(
        () => parseSchwabCsv({
          content,
          fileName: FILE_NAME,
          sourceHash: "source-hash-invalid",
          sourceReference: "import-invalid",
        }),
        (error: unknown) => (
          error instanceof ApplicationError
          && error.code === "IMPORT_VALIDATION_FAILED"
          && error.statusCode === 422
        ),
      );
    }
  });
});
