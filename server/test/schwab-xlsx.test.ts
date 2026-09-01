import assert from "node:assert/strict";
import { describe, it } from "node:test";

import ExcelJS from "exceljs";

import {
  ApplicationError,
  createImportPreview,
} from "../src/application/index.js";
import { SCHWAB_TRANSACTION_HEADERS } from "../src/infrastructure/imports/schwab-table.js";
import { parseSchwabXlsx } from "../src/infrastructure/imports/schwab-xlsx.js";

const FILE_NAME = "Individual_TST001_Transactions_20260831-090000.xlsx";

/** 建立一个仅含标准 Transactions 工作表的最小工作簿。 */
function createWorkbook(): { workbook: ExcelJS.Workbook; sheet: ExcelJS.Worksheet } {
  const workbook = new ExcelJS.Workbook();
  const sheet = workbook.addWorksheet("Transactions");
  sheet.addRow([...SCHWAB_TRANSACTION_HEADERS]);
  sheet.addRow([
    new Date(Date.UTC(2026, 7, 30)),
    "Buy",
    "LYRA",
    "LYRA CORP",
    2,
    10,
    0.01,
    -20.01,
  ]);
  return { workbook, sheet };
}

/** 写入内存并调用生产 XLSX 适配器。 */
async function parseWorkbook(workbook: ExcelJS.Workbook) {
  const excelBuffer = await workbook.xlsx.writeBuffer();
  return parseSchwabXlsx({
    content: Buffer.from(excelBuffer),
    fileName: FILE_NAME,
    sourceHash: "xlsx-source-hash",
    sourceReference: "import-xlsx-1",
  });
}

describe("Schwab XLSX adapter", () => {
  it("读取日期和数值单元格，并复用统一预览内核", async () => {
    const { workbook } = createWorkbook();
    const batch = await parseWorkbook(workbook);
    const preview = createImportPreview({
      batch,
      parserVersion: "schwab-xlsx-v1",
      baseLedgerVersion: 8,
      existingTransactionHashes: new Set<string>(),
    });

    assert.equal(batch.account, "TST001");
    assert.equal(batch.sourceType, "excel");
    assert.equal(batch.rows[0]?.Date, "08/30/2026");
    assert.equal(batch.rows[0]?.Amount, "-20.01");
    assert.equal(preview.totalRows, 1);
    assert.equal(preview.transactions[0]?.amount?.toFixed(2), "-20.01");
    assert.equal(preview.transactions[0]?.sourceReference, "import-xlsx-1");
  });

  it("拒绝隐藏交易、公式和多个候选工作表", async () => {
    const hidden = createWorkbook();
    hidden.sheet.getRow(2).hidden = true;
    await assert.rejects(
      () => parseWorkbook(hidden.workbook),
      (error: unknown) => error instanceof ApplicationError
        && error.code === "IMPORT_VALIDATION_FAILED",
    );

    const formula = createWorkbook();
    formula.sheet.getCell("H2").value = { formula: "-E2*F2-G2", result: -20.01 };
    await assert.rejects(
      () => parseWorkbook(formula.workbook),
      (error: unknown) => error instanceof ApplicationError
        && error.code === "IMPORT_VALIDATION_FAILED",
    );

    const ambiguous = createWorkbook();
    const second = ambiguous.workbook.addWorksheet("Transactions Copy");
    second.addRow([...SCHWAB_TRANSACTION_HEADERS]);
    second.addRow(["08/30/2026", "Buy", "LYRA", "LYRA CORP", "2", "10", "0.01", "-20.01"]);
    await assert.rejects(
      () => parseWorkbook(ambiguous.workbook),
      (error: unknown) => error instanceof ApplicationError
        && error.code === "IMPORT_AMBIGUOUS_SHEET",
    );
  });
});
