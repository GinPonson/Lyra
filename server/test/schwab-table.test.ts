import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { ApplicationError } from "../src/application/index.js";
import {
  SCHWAB_TRANSACTION_HEADERS,
  assertSchwabTransactionHeaders,
  mapSchwabTransactionRow,
} from "../src/infrastructure/imports/schwab-table.js";

describe("Schwab tabular adapter", () => {
  it("接受带 UTF-8 BOM 的标准八列表头并按物理顺序映射行", () => {
    assert.doesNotThrow(() => assertSchwabTransactionHeaders([
      `\uFEFF${SCHWAB_TRANSACTION_HEADERS[0]}`,
      ...SCHWAB_TRANSACTION_HEADERS.slice(1),
    ]));
    assert.deepEqual(
      mapSchwabTransactionRow([
        "08/30/2026",
        "Buy",
        "LYRA",
        "LYRA CORP",
        "2",
        "$10.00",
        "$0.01",
        "-$20.01",
      ], "transactions.csv 第1行"),
      {
        Date: "08/30/2026",
        Action: "Buy",
        Symbol: "LYRA",
        Description: "LYRA CORP",
        Quantity: "2",
        Price: "$10.00",
        "Fees & Comm": "$0.01",
        Amount: "-$20.01",
      },
    );
  });

  it("以稳定错误拒绝列重排、未知列及非文本单元格", () => {
    const reordered = [
      SCHWAB_TRANSACTION_HEADERS[1],
      SCHWAB_TRANSACTION_HEADERS[0],
      ...SCHWAB_TRANSACTION_HEADERS.slice(2),
    ];
    assert.throws(
      () => assertSchwabTransactionHeaders(reordered),
      (error: unknown) => {
        assert.ok(error instanceof ApplicationError);
        assert.equal(error.code, "IMPORT_UNKNOWN_HEADERS");
        assert.equal(error.statusCode, 422);
        return true;
      },
    );
    assert.throws(
      () => mapSchwabTransactionRow([
        "08/30/2026", "Buy", "LYRA", "LYRA CORP", 2, "$10.00", "", "-$20.00",
      ], "transactions.xlsx 第1行"),
      (error: unknown) => {
        assert.ok(error instanceof ApplicationError);
        assert.equal(error.code, "IMPORT_VALIDATION_FAILED");
        return true;
      },
    );
  });
});
