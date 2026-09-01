import { Decimal } from "decimal.js";

import { IngestError } from "./errors.js";
import type { LocalDate, ParsedDateField, ParsedOptionSymbol } from "./types.js";

/**
 * 与 Python `OPTION_SYMBOL_RE` 保持相同业务约束的 Schwab 期权代码格式。
 * 标的允许包含点号，但不允许空白；日期和 C/P 标记必须完整存在。
 */
const OPTION_SYMBOL_PATTERN = /^(\S+) (\d{2}\/\d{2}\/\d{4}) ([0-9]+(?:\.[0-9]+)?) ([CP])$/;

/** 从 Schwab 导出文件名提取掩码账户标识，例如 `XXX276`。 */
const ACCOUNT_PATTERN = /Individual_([^_]+)_/;

/**
 * 解析 Schwab 金额、数量、价格或费用字段。
 *
 * 规则与现有 Python 版本一致：移除美元符号和千位逗号，保留正负号；
 * 纯空白返回 `null`。无法构造 Decimal 时直接抛出 `IngestError`，调用方
 * 必须中止当前文件预览，不能跳过该字段。
 *
 * @param raw - CSV 或 Excel 单元格转换得到的原始文本。
 * @returns 精确 Decimal；空字段返回 `null`。
 * @throws IngestError 当文本不是合法十进制数时抛出。
 */
export function parseMoney(raw: string): Decimal | null {
  const normalized = raw.trim().replaceAll("$", "").replaceAll(",", "");
  if (normalized.length === 0) {
    return null;
  }

  try {
    return new Decimal(normalized);
  } catch (error: unknown) {
    // Decimal.js 的异常类型不是稳定公共契约，因此只在此边界转换为领域错误。
    if (error instanceof Error) {
      throw new IngestError(`无法解析金额: ${JSON.stringify(raw)}`);
    }
    throw error;
  }
}

/**
 * 解析 Schwab Date 字段。
 *
 * 支持 `MM/DD/YYYY` 和 `MM/DD/YYYY as of MM/DD/YYYY`。后者的首个日期
 * 是记录日期，`as of` 后的日期是交易生效日期。
 *
 * @param raw - Schwab Date 列原始文本。
 * @returns 记录日期与交易生效日期。
 * @throws IngestError 当格式或日历日期无效时抛出。
 */
export function parseDateField(raw: string): ParsedDateField {
  const text = raw.trim();
  const parts = text.split(" as of ");
  if (parts.length > 2) {
    throw new IngestError(`无法解析日期: ${JSON.stringify(raw)}`);
  }

  const recordRaw = parts[0];
  const transactionRaw = parts[1] ?? recordRaw;
  if (recordRaw === undefined || transactionRaw === undefined) {
    throw new IngestError(`无法解析日期: ${JSON.stringify(raw)}`);
  }

  try {
    return {
      recordDate: parseUsDate(recordRaw),
      transactionDate: parseUsDate(transactionRaw),
    };
  } catch (error: unknown) {
    if (error instanceof RangeError) {
      throw new IngestError(`无法解析日期: ${JSON.stringify(raw)}`);
    }
    throw error;
  }
}

/**
 * 尝试解析 Schwab 文本期权代码。
 *
 * 普通股票代码不属于错误，返回 `null` 交由交易动作决定资产类型；只有在
 * 调用方确认动作必须是期权时，未命中格式才应升级为导入错误。
 *
 * @param symbol - Schwab Symbol 字段。
 * @returns 期权合约字段；非期权格式返回 `null`。
 */
export function parseOptionSymbol(symbol: string): ParsedOptionSymbol | null {
  const match = OPTION_SYMBOL_PATTERN.exec(symbol.trim());
  if (match === null) {
    return null;
  }

  const [, underlying, expiryRaw, strikeRaw, optionType] = match;
  if (
    underlying === undefined
    || expiryRaw === undefined
    || strikeRaw === undefined
    || (optionType !== "C" && optionType !== "P")
  ) {
    // 正则和解构定义理论上保证这些字段存在；显式失败可防止规则未来修改后带病运行。
    throw new IngestError(`期权 Symbol 格式无法解析: ${JSON.stringify(symbol)}`);
  }

  try {
    return {
      underlying,
      expiry: parseUsDate(expiryRaw),
      strike: new Decimal(strikeRaw),
      optionType,
    };
  } catch (error: unknown) {
    if (error instanceof Error) {
      throw new IngestError(`期权 Symbol 格式无法解析: ${JSON.stringify(symbol)}`);
    }
    throw error;
  }
}

/**
 * 从 Schwab 官方导出文件名提取账户标识。
 *
 * @param fileName - 仅文件名，不应传入完整文件系统路径。
 * @returns 文件名中 `Individual_` 与下一个下划线之间的账户标识。
 * @throws IngestError 当文件名不符合已确认的 Schwab 命名格式时抛出。
 */
export function parseAccount(fileName: string): string {
  const match = ACCOUNT_PATTERN.exec(fileName);
  const account = match?.[1];
  if (account === undefined) {
    throw new IngestError(
      `无法从文件名解析账户: ${JSON.stringify(fileName)}(期望形如 Individual_XXX276_...)`,
    );
  }
  return account;
}

/**
 * 将美式日历日期转换为无时区 ISO 日期。
 *
 * @param raw - `M/D/YYYY` 或 `MM/DD/YYYY` 文本；宽度行为与 Python `int` 解析一致。
 * @returns `YYYY-MM-DD`。
 * @throws RangeError 当格式、数值或实际日历日期无效时抛出。
 */
function parseUsDate(raw: string): LocalDate {
  const parts = raw.split("/");
  if (parts.length !== 3) {
    throw new RangeError("invalid US date format");
  }

  const [monthRaw, dayRaw, yearRaw] = parts;
  if (monthRaw === undefined || dayRaw === undefined || yearRaw === undefined) {
    throw new RangeError("invalid US date parts");
  }
  if (!/^\d{1,2}$/.test(monthRaw) || !/^\d{1,2}$/.test(dayRaw) || !/^\d{4}$/.test(yearRaw)) {
    throw new RangeError("invalid US date digits");
  }

  const month = Number.parseInt(monthRaw, 10);
  const day = Number.parseInt(dayRaw, 10);
  const year = Number.parseInt(yearRaw, 10);
  const candidate = new Date(Date.UTC(year, month - 1, day));

  // Date.UTC 会把 02/30 自动滚动到三月，因此必须反向比对全部日历字段。
  if (
    candidate.getUTCFullYear() !== year
    || candidate.getUTCMonth() !== month - 1
    || candidate.getUTCDate() !== day
  ) {
    throw new RangeError("invalid calendar date");
  }

  return `${yearRaw}-${monthRaw.padStart(2, "0")}-${dayRaw.padStart(2, "0")}`;
}
