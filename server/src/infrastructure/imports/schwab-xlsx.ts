import ExcelJS, {
  type Cell,
  type Row,
  type Worksheet,
} from "exceljs";

import { ApplicationError } from "../../application/index.js";
import { parseAccount, type TransactionBatchInput } from "../../domain/ledger/index.js";
import {
  createSchwabUnknownHeadersError,
  isSchwabTransactionHeaders,
  mapSchwabTransactionRow,
} from "./schwab-table.js";

/** 已完成上传签名与大小检查的 Schwab XLSX 输入。 */
export interface ParseSchwabXlsxInput {
  content: Uint8Array;
  fileName: string;
  sourceHash: string;
  sourceReference: string;
}

interface TransactionWorksheet {
  worksheet: Worksheet;
  headerRowNumber: number;
}

/**
 * 读取唯一可见的 Schwab Transactions 工作表并转换为统一批次输入。
 *
 * 适配器不执行任何金融计算；公式、隐藏交易行、隐藏候选工作表和多个候选
 * 工作表会阻断预览，避免用户看不到的内容或不明确来源进入账本。
 *
 * @param input - 已受大小和 XLSX ZIP 签名限制的原始工作簿字节与来源元数据。
 * @returns 可直接交给统一导入预览内核的标准批次输入。
 */
export async function parseSchwabXlsx(
  input: ParseSchwabXlsxInput,
): Promise<TransactionBatchInput> {
  const workbook = new ExcelJS.Workbook();
  try {
    await workbook.xlsx.load(Buffer.from(input.content));
  } catch (error: unknown) {
    throw new ApplicationError({
      code: "IMPORT_VALIDATION_FAILED",
      message: `${input.fileName}: XLSX 工作簿无法读取或已加密`,
      statusCode: 422,
      retryable: false,
      cause: error,
    });
  }

  const candidates = workbook.worksheets
    .map(findTransactionWorksheet)
    .filter((candidate): candidate is TransactionWorksheet => candidate !== null);
  const hiddenCandidate = candidates.find(({ worksheet }) => worksheet.state !== "visible");
  if (hiddenCandidate !== undefined) {
    throw workbookValidationError(
      input.fileName,
      "交易工作表不可隐藏",
      { worksheet: hiddenCandidate.worksheet.name },
    );
  }
  const visibleCandidates = candidates.filter(({ worksheet }) => worksheet.state === "visible");
  if (visibleCandidates.length === 0) {
    throw createSchwabUnknownHeadersError([]);
  }
  if (visibleCandidates.length > 1) {
    throw new ApplicationError({
      code: "IMPORT_AMBIGUOUS_SHEET",
      message: `${input.fileName}: 存在多个 Schwab Transactions 候选工作表`,
      statusCode: 422,
      retryable: false,
      details: {
        worksheets: visibleCandidates.map(({ worksheet }) => worksheet.name),
      },
    });
  }

  const selected = visibleCandidates[0];
  if (selected === undefined) {
    throw new Error("XLSX 候选工作表选择内部状态不一致");
  }
  const rows = readTransactionRows(input.fileName, selected);
  return {
    fileName: input.fileName,
    account: parseAccount(input.fileName),
    sourceHash: input.sourceHash,
    sourceType: "excel",
    sourceReference: input.sourceReference,
    rows,
  };
}

/** 以每个工作表首个非空行作为唯一候选表头。 */
function findTransactionWorksheet(worksheet: Worksheet): TransactionWorksheet | null {
  for (let rowNumber = 1; rowNumber <= worksheet.rowCount; rowNumber += 1) {
    const row = worksheet.getRow(rowNumber);
    if (!row.hasValues) {
      continue;
    }
    const headers = readHeaderValues(row);
    return isSchwabTransactionHeaders(headers)
      ? { worksheet, headerRowNumber: rowNumber }
      : null;
  }
  return null;
}

/** 读取候选表头，并忽略没有值的尾部格式化单元格。 */
function readHeaderValues(row: Row): unknown[] {
  const values: unknown[] = [];
  const lastColumn = Math.max(8, row.cellCount);
  for (let column = 1; column <= lastColumn; column += 1) {
    const value = row.getCell(column).value;
    values.push(value ?? "");
  }
  while (values.length > 8 && values.at(-1) === "") {
    values.pop();
  }
  return values;
}

/** 按工作簿物理行顺序读取可见、非公式的八列交易。 */
function readTransactionRows(
  fileName: string,
  selected: TransactionWorksheet,
): TransactionBatchInput["rows"] {
  const rows: TransactionBatchInput["rows"][number][] = [];
  for (
    let rowNumber = selected.headerRowNumber + 1;
    rowNumber <= selected.worksheet.rowCount;
    rowNumber += 1
  ) {
    const row = selected.worksheet.getRow(rowNumber);
    if (!row.hasValues || isEmptyTransactionRow(row)) {
      continue;
    }
    const where = `${fileName} 工作表 ${selected.worksheet.name} 第${rowNumber}行`;
    if (row.hidden) {
      throw workbookValidationError(fileName, `${where}: 交易行不可隐藏`);
    }
    const values: string[] = [];
    const lastColumn = Math.max(8, row.cellCount);
    for (let column = 1; column <= lastColumn; column += 1) {
      values.push(cellToText(row.getCell(column), fileName, where));
    }
    while (values.length > 8 && values.at(-1) === "") {
      values.pop();
    }
    rows.push(mapSchwabTransactionRow(values, where));
  }
  return rows;
}

/** 空物理行不产生交易序号。 */
function isEmptyTransactionRow(row: Row): boolean {
  for (let column = 1; column <= Math.max(8, row.cellCount); column += 1) {
    const cell = row.getCell(column);
    if (cell.value !== null && cell.value !== undefined && cell.text.length > 0) {
      return false;
    }
  }
  return true;
}

/** 将受支持的 Excel 单元格转换为领域层可解析文本。 */
function cellToText(cell: Cell, fileName: string, where: string): string {
  if (cell.isMerged || cell.type === ExcelJS.ValueType.Formula) {
    throw workbookValidationError(
      fileName,
      `${where}: ${cell.address} 不允许公式或合并单元格`,
    );
  }
  const value = cell.value;
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw workbookValidationError(fileName, `${where}: ${cell.address} 数值无效`);
    }
    return cell.text.length > 0 ? cell.text : String(value);
  }
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) {
      throw workbookValidationError(fileName, `${where}: ${cell.address} 日期无效`);
    }
    const month = String(value.getUTCMonth() + 1).padStart(2, "0");
    const day = String(value.getUTCDate()).padStart(2, "0");
    return `${month}/${day}/${value.getUTCFullYear()}`;
  }
  if (typeof value === "object" && "richText" in value) {
    return cell.text;
  }
  throw workbookValidationError(
    fileName,
    `${where}: ${cell.address} 必须是文本、数值、日期或空值`,
  );
}

/** 创建不包含底层 ZIP/XML 错误或单元格值的工作簿校验错误。 */
function workbookValidationError(
  fileName: string,
  message: string,
  details: Readonly<Record<string, unknown>> | null = null,
): ApplicationError {
  return new ApplicationError({
    code: "IMPORT_VALIDATION_FAILED",
    message: message.startsWith(fileName) ? message : `${fileName}: ${message}`,
    statusCode: 422,
    retryable: false,
    details,
  });
}
