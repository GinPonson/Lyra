import { formatIsoDate, parseIsoDate } from "../domain/analytics/index.js";

/** 官方文件对账提醒的默认周期：一个自然月。 */
export const DEFAULT_OFFICIAL_IMPORT_INTERVAL_MONTHS = 1;

/** 单账户官方文件对账提醒的确定性状态。 */
export interface OfficialImportReminderPlan {
  accountId: string;
  status: "disabled" | "scheduled" | "due";
  intervalMonths: number;
  /** 禁用时为 null；其他状态为下次应导入的 UTC 日历日期。 */
  dueDate: string | null;
}

/**
 * 根据最近一次成功的 Schwab 官方文件导入生成单账户提醒计划。
 *
 * @param input.accountId - App 使用的明确账户 ID。
 * @param input.enabled - 用户是否启用该账户的官方对账提醒。
 * @param input.intervalMonths - 可调整的自然月周期，缺省为一个月。
 * @param input.lastSuccessfulImportDate - 最近成功提交官方文件的 UTC 日历日期。
 * @param input.today - 服务端可信时钟对应的 UTC 日历日期。
 * @returns 禁用、待到期或已到期状态；不把 Gmail 同步误算成官方导入。
 */
export function planOfficialImportReminder(input: {
  accountId: string;
  enabled: boolean;
  intervalMonths?: number;
  lastSuccessfulImportDate: string | null;
  today: string;
}): OfficialImportReminderPlan {
  const accountId = input.accountId.trim();
  if (accountId.length === 0) {
    throw new RangeError("官方文件对账提醒的账户 ID 不能为空");
  }
  const intervalMonths = input.intervalMonths ?? DEFAULT_OFFICIAL_IMPORT_INTERVAL_MONTHS;
  if (!Number.isSafeInteger(intervalMonths) || intervalMonths < 1 || intervalMonths > 24) {
    throw new RangeError("官方文件对账提醒周期必须是 1 到 24 个自然月");
  }
  if (!input.enabled) {
    return { accountId, status: "disabled", intervalMonths, dueDate: null };
  }

  const today = parseIsoDate(input.today);
  const dueDate = input.lastSuccessfulImportDate === null
    ? formatIsoDate(today)
    : formatIsoDate(addUtcCalendarMonths(
      parseIsoDate(input.lastSuccessfulImportDate),
      intervalMonths,
    ));
  return {
    accountId,
    status: input.today >= dueDate ? "due" : "scheduled",
    intervalMonths,
    dueDate,
  };
}

/** 按 UTC 自然月递增，并把月末日期钳制到目标月的最后一天。 */
function addUtcCalendarMonths(date: Date, months: number): Date {
  const targetMonthStart = new Date(Date.UTC(
    date.getUTCFullYear(),
    date.getUTCMonth() + months,
    1,
  ));
  const targetMonthEnd = new Date(Date.UTC(
    targetMonthStart.getUTCFullYear(),
    targetMonthStart.getUTCMonth() + 1,
    0,
  ));
  targetMonthStart.setUTCDate(Math.min(date.getUTCDate(), targetMonthEnd.getUTCDate()));
  return targetMonthStart;
}
