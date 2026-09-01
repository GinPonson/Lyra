import { parseIsoDate } from "../../domain/analytics/index.js";
import {
  SCHWAB_ECONFIRM_SENDER,
  SCHWAB_ECONFIRM_SUBJECT,
} from "../../domain/gmail/index.js";
import type { LocalDate } from "../../domain/ledger/index.js";

/** 用户确认范围后可交给 Gmail messages.list 的有界搜索计划。 */
export interface GmailBoundedSearchPlan {
  query: string;
  afterDate: LocalDate;
  maximumMessages: number;
}

/**
 * 建立只面向已确认 Schwab eConfirm 来源的 Gmail 查询。
 *
 * 使用 UTC epoch 秒而不是模糊日期文本，避免 Gmail 搜索时区造成起始日漂移；
 * maximumMessages 是整个扫描任务的硬上限，不是自动扩展的分页建议。
 *
 * @param afterDate - 用户明确确认的最早扫描日，按 UTC 午夜计算。
 * @param maximumMessages - 本次扫描最多读取的消息总数。
 */
export function buildSchwabEconfirmSearch(
  afterDate: LocalDate,
  maximumMessages: number,
): GmailBoundedSearchPlan {
  if (!Number.isSafeInteger(maximumMessages) || maximumMessages <= 0) {
    throw new RangeError("Gmail 有界扫描上限必须是正安全整数");
  }
  const afterEpochSeconds = Math.floor(parseIsoDate(afterDate).getTime() / 1_000);
  return {
    query: (
      `from:${SCHWAB_ECONFIRM_SENDER} `
      + `subject:\"${SCHWAB_ECONFIRM_SUBJECT}\" `
      + `after:${afterEpochSeconds}`
    ),
    afterDate,
    maximumMessages,
  };
}
