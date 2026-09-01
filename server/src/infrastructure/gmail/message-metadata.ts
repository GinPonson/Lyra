import { createHash } from "node:crypto";
import { normalizeGmailHistoryId } from "../../domain/gmail/index.js";
import type { EmailTrade, GmailMessage } from "../../domain/gmail/index.js";
import { IngestError } from "../../domain/ledger/index.js";

/** 与 gmail_messages 表 parsed 状态对齐的无正文持久化记录。 */
export interface ParsedGmailMessageMetadata {
  connectionId: string;
  messageId: string;
  threadId: string | null;
  receivedAt: string | null;
  contentHash: string;
  accountId: string | null;
  parseStatus: "parsed";
  errorCode: null;
  transactionCount: number;
  historyId: string | null;
  syncedAt: string;
}

/** 解析拒绝或账户冲突时持久化的无正文消息元数据。 */
export interface UnsuccessfulGmailMessageMetadata {
  connectionId: string;
  messageId: string;
  threadId: string | null;
  receivedAt: string | null;
  contentHash: string;
  accountId: string | null;
  parseStatus: "rejected" | "conflict";
  errorCode: "GMAIL_MESSAGE_REJECTED" | "ACCOUNT_MAPPING_REQUIRED" | "GMAIL_MESSAGE_CONFLICT";
  transactionCount: 0;
  historyId: string | null;
  syncedAt: string;
}

/**
 * 为成功解析的 eConfirm 建立不含邮件正文的确定性持久化元数据。
 *
 * contentHash 使用已提取的发件人、主题和规范换行正文，仅用于检测同一 message
 * id 内容变化；正文在计算完成后不得进入数据库、日志或 Agent 上下文。
 */
export function createParsedGmailMessageMetadata(input: {
  connectionId: string;
  threadId: string | null;
  message: GmailMessage;
  trades: readonly EmailTrade[];
  accountId: string | null;
  historyId: string | null;
  syncedAt: Date;
}): ParsedGmailMessageMetadata {
  const base = createBaseMetadata(input);

  return {
    ...base,
    accountId: nullableText(input.accountId),
    parseStatus: "parsed",
    errorCode: null,
    transactionCount: input.trades.length,
  };
}

/**
 * 为确定性解析拒绝或账户映射冲突建立无正文持久化记录。
 *
 * 失败详情不得进入该记录；稳定错误码足以让 App 展示后续动作。消息摘要仍与
 * 成功路径一致，使同一 message ID 的内容变化可以被检测。
 */
export function createUnsuccessfulGmailMessageMetadata(input: {
  connectionId: string;
  threadId: string | null;
  message: GmailMessage;
  accountId: string | null;
  historyId: string | null;
  syncedAt: Date;
  errorCode: UnsuccessfulGmailMessageMetadata["errorCode"];
}): UnsuccessfulGmailMessageMetadata {
  return {
    ...createBaseMetadata(input),
    accountId: nullableText(input.accountId),
    parseStatus: input.errorCode === "GMAIL_MESSAGE_REJECTED" ? "rejected" : "conflict",
    errorCode: input.errorCode,
    transactionCount: 0,
  };
}

/** 成功与失败元数据共用完全相同的身份、时间和内容摘要规则。 */
function createBaseMetadata(input: {
  connectionId: string;
  threadId: string | null;
  message: GmailMessage;
  historyId: string | null;
  syncedAt: Date;
}): Pick<
  ParsedGmailMessageMetadata,
  "connectionId" | "messageId" | "threadId" | "receivedAt" | "contentHash" | "historyId" | "syncedAt"
> {
  const canonicalContent = JSON.stringify({
    sender: input.message.sender.trim().toLowerCase(),
    subject: input.message.subject.trim(),
    body: input.message.body.replace(/\r\n?/g, "\n"),
  });
  return {
    connectionId: requireText(input.connectionId, "Gmail 连接 ID"),
    messageId: requireText(input.message.messageId, "Gmail message ID"),
    threadId: nullableText(input.threadId),
    receivedAt: input.message.emailTimestamp === null
      ? null
      : parseEmailTimestamp(input.message.emailTimestamp),
    contentHash: createHash("sha256").update(canonicalContent, "utf8").digest("hex"),
    historyId: input.historyId === null ? null : normalizeGmailHistoryId(input.historyId),
    syncedAt: requireTimestamp(input.syncedAt, "Gmail 同步时间"),
  };
}

/** Gmail Date header 或 internalDate 毫秒值必须能确定为有效 UTC 时间。 */
function parseEmailTimestamp(value: string): string {
  const normalized = value.trim();
  const epochMilliseconds = /^\d+$/.test(normalized)
    ? Number(normalized)
    : Date.parse(normalized);
  if (!Number.isFinite(epochMilliseconds)) {
    throw new IngestError("Gmail 邮件时间戳无效");
  }
  const timestamp = new Date(epochMilliseconds);
  if (Number.isNaN(timestamp.getTime())) {
    throw new IngestError("Gmail 邮件时间戳无效");
  }
  return timestamp.toISOString();
}

/** 去除边界空白并拒绝空必填标识。 */
function requireText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}

/** 规范可空标识，空白等价于数据库 null。 */
function nullableText(value: string | null): string | null {
  if (value === null) {
    return null;
  }
  const normalized = value.trim();
  return normalized.length === 0 ? null : normalized;
}

/** 校验可信 Date 并转换为 UTC ISO 时间戳。 */
function requireTimestamp(value: Date, label: string): string {
  if (Number.isNaN(value.getTime())) {
    throw new RangeError(`${label}无效`);
  }
  return value.toISOString();
}
