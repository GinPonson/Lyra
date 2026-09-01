/** 一天的毫秒数；分析日期始终按 UTC 日历日计算。 */
export const MILLISECONDS_PER_DAY = 86_400_000;

/**
 * 严格解析 ISO 日历日期并拒绝 JavaScript Date 的自动进位。
 *
 * @param value - `YYYY-MM-DD`。
 * @returns UTC 午夜 Date，仅用于日期算术。
 * @throws RangeError 当格式或实际日历日期无效时抛出。
 */
export function parseIsoDate(value: string): Date {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (match === null) {
    throw new RangeError(`无法解析日期: ${value}`);
  }
  const [, yearRaw, monthRaw, dayRaw] = match;
  const year = Number(yearRaw);
  const month = Number(monthRaw);
  const day = Number(dayRaw);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (
    date.getUTCFullYear() !== year
    || date.getUTCMonth() !== month - 1
    || date.getUTCDate() !== day
  ) {
    throw new RangeError(`无法解析日期: ${value}`);
  }
  return date;
}

/** 把 UTC 日历日期格式化为 ISO 日期。 */
export function formatIsoDate(value: Date): string {
  return value.toISOString().slice(0, 10);
}

/** 校验可选日期范围，确保起始日不晚于截止日。 */
export function validateDateRange(fromDate?: string, toDate?: string): void {
  if (fromDate !== undefined) {
    parseIsoDate(fromDate);
  }
  if (toDate !== undefined) {
    parseIsoDate(toDate);
  }
  if (fromDate !== undefined && toDate !== undefined && fromDate > toDate) {
    throw new RangeError("起始日期不能晚于截止日期");
  }
}

/** 判断 ISO 日期是否位于可选闭区间内。 */
export function isDateInRange(date: string, fromDate?: string, toDate?: string): boolean {
  return (fromDate === undefined || date >= fromDate)
    && (toDate === undefined || date <= toDate);
}

