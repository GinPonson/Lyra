import { CsvError, parse } from "csv-parse/sync";

import { ApplicationError } from "../../application/index.js";
import { parseAccount, type TransactionBatchInput } from "../../domain/ledger/index.js";
import {
  assertSchwabTransactionHeaders,
  mapSchwabTransactionRow,
} from "./schwab-table.js";

/** 已完成上传大小限制与 SHA-256 计算的 Schwab CSV 输入。 */
export interface ParseSchwabCsvInput {
  content: Uint8Array;
  fileName: string;
  sourceHash: string;
  sourceReference: string;
}

/**
 * 严格解析 Schwab Transactions CSV，并转换为统一批次输入。
 *
 * CSV 引号、转义、换行与列数由 `csv-parse` 处理；本适配器只负责 UTF-8、
 * 标准八列表头、账户线索和固定列映射。金额、动作、期权及哈希仍由既有领域
 * 标准化器统一处理，避免 CSV 与 Excel 形成两套金融规则。
 *
 * @param input - 已受上传大小限制的原始字节及服务端生成的来源元数据。
 * @returns 可直接交给 `createImportPreview` 的标准批次输入。
 * @throws ApplicationError 当编码、CSV 语法、列数或表头无效时抛出。
 */
export function parseSchwabCsv(input: ParseSchwabCsvInput): TransactionBatchInput {
  const text = decodeUtf8(input.content, input.fileName);
  let records: string[][];
  try {
    records = parse(text, {
      bom: true,
      columns: false,
      relax_column_count: false,
      skip_empty_lines: true,
      trim: false,
    });
  } catch (error: unknown) {
    if (error instanceof CsvError) {
      throw csvSyntaxError(input.fileName, error);
    }
    throw error;
  }

  const headers = records[0] ?? [];
  assertSchwabTransactionHeaders(headers);
  const rows = records.slice(1).map((record, index) => (
    mapSchwabTransactionRow(record, `${input.fileName} 第${index + 1}行`)
  ));

  return {
    fileName: input.fileName,
    account: parseAccount(input.fileName),
    sourceHash: input.sourceHash,
    sourceType: "csv",
    sourceReference: input.sourceReference,
    rows,
  };
}

/** 使用 fatal UTF-8 解码，禁止损坏字节被静默替换为 U+FFFD。 */
function decodeUtf8(content: Uint8Array, fileName: string): string {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(content);
  } catch (error: unknown) {
    throw new ApplicationError({
      code: "IMPORT_VALIDATION_FAILED",
      message: `${fileName}: CSV 必须使用有效 UTF-8 编码`,
      statusCode: 422,
      retryable: false,
      cause: error,
    });
  }
}

/** 将第三方解析器错误收窄为不暴露原始单元格内容的稳定 API 错误。 */
function csvSyntaxError(fileName: string, error: CsvError): ApplicationError {
  const line = typeof error.lines === "number" && Number.isSafeInteger(error.lines)
    ? error.lines
    : null;
  return new ApplicationError({
    code: "IMPORT_VALIDATION_FAILED",
    message: line === null
      ? `${fileName}: CSV 语法无效`
      : `${fileName}: CSV 第${line}物理行语法无效`,
    statusCode: 422,
    retryable: false,
    details: line === null ? null : { line },
    cause: error,
  });
}
