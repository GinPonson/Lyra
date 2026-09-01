import { ApplicationError } from "../../application/index.js";
import type { RawTransactionFields } from "../../domain/ledger/index.js";

/** Schwab Transactions 导出的唯一受支持列集合及物理顺序。 */
export const SCHWAB_TRANSACTION_HEADERS = [
  "Date",
  "Action",
  "Symbol",
  "Description",
  "Quantity",
  "Price",
  "Fees & Comm",
  "Amount",
] as const;

/** 判断一行是否与 Schwab 标准八列表头及顺序完全一致。 */
export function isSchwabTransactionHeaders(headers: readonly unknown[]): boolean {
  const normalized = normalizeHeaders(headers);
  return normalized !== null
    && normalized.length === SCHWAB_TRANSACTION_HEADERS.length
    && normalized.every((header, index) => header === SCHWAB_TRANSACTION_HEADERS[index]);
}

/**
 * 严格校验 CSV/Excel 标准解析器返回的表头。
 *
 * 不允许重排、缺列或额外列，避免上游格式变化把金额等字段静默映射到错误列。
 * UTF-8 BOM 只能出现在第一个表头开头，并会在比较前移除。
 *
 * @param headers - 解析器按文件物理顺序返回的表头。
 */
export function assertSchwabTransactionHeaders(headers: readonly unknown[]): void {
  if (!isSchwabTransactionHeaders(headers)) {
    throw createSchwabUnknownHeadersError(headers);
  }
}

/**
 * 把已通过表头检查的一行八列值映射为领域标准化器的原始字段对象。
 *
 * @param values - CSV/Excel 解析器输出的单行值，必须全部为字符串。
 * @param where - 不含服务端路径的用户可见行位置。
 */
export function mapSchwabTransactionRow(
  values: readonly unknown[],
  where: string,
): RawTransactionFields {
  if (where.trim().length === 0) {
    throw new RangeError("Schwab 表格行位置不能为空");
  }
  if (
    values.length !== SCHWAB_TRANSACTION_HEADERS.length
    || values.some((value) => typeof value !== "string")
  ) {
    throw new ApplicationError({
      code: "IMPORT_VALIDATION_FAILED",
      message: `${where}: 必须包含八个文本字段`,
      statusCode: 422,
      retryable: false,
    });
  }

  // 长度和字符串类型已经整体检查，按固定下标读取不会产生隐式列映射。
  const [date, action, symbol, description, quantity, price, fees, amount] = values as readonly string[];
  if (
    date === undefined
    || action === undefined
    || symbol === undefined
    || description === undefined
    || quantity === undefined
    || price === undefined
    || fees === undefined
    || amount === undefined
  ) {
    throw new Error("Schwab 八列映射内部状态不一致");
  }
  return {
    Date: date,
    Action: action,
    Symbol: symbol,
    Description: description,
    Quantity: quantity,
    Price: price,
    "Fees & Comm": fees,
    Amount: amount,
  };
}

/** 创建不泄露非文本单元格内容的稳定表头错误。 */
export function createSchwabUnknownHeadersError(
  headers: readonly unknown[],
): ApplicationError {
  return new ApplicationError({
    code: "IMPORT_UNKNOWN_HEADERS",
    message: "导入文件表头与 Schwab Transactions 标准八列不一致",
    statusCode: 422,
    retryable: false,
    details: {
      expected_headers: SCHWAB_TRANSACTION_HEADERS,
      actual_headers: headers.map((header) => (
        typeof header === "string" ? header : `<${typeof header}>`
      )),
    },
  });
}

/** 规范表头文本；非文本单元格不参与宽松转换。 */
function normalizeHeaders(headers: readonly unknown[]): string[] | null {
  const normalized: string[] = [];
  for (const [index, header] of headers.entries()) {
    if (typeof header !== "string") {
      return null;
    }
    normalized.push(index === 0 ? header.replace(/^\uFEFF/, "") : header);
  }
  return normalized;
}
