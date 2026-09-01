import { createHash } from "node:crypto";

import { simpleParser, type HeaderValue, type ParsedMail } from "mailparser";

import type { GmailMessage } from "../../domain/gmail/index.js";
import {
  SCHWAB_ECONFIRM_SUBJECT,
  SCHWAB_SENDER_PATTERN,
} from "../../domain/gmail/index.js";
import { IngestError } from "../../domain/ledger/index.js";
import { normalizeEmailBodyCandidates } from "./full-message.js";

/** 已经过调用方大小限制的原始 RFC 5322/MIME 输入。 */
export interface ParseRfcMimeMessageInput {
  content: Uint8Array;
  sourceName: string;
}

/**
 * 使用成熟 MIME 解析器把原始 `.eml` 转换为既有 GmailMessage 领域输入。
 *
 * 本适配器只读取内联 plain/HTML 正文；附件不会进入候选、持久化或 Agent
 * 上下文。调用方必须先完成文件大小限制，解析完成后继续复用 eConfirm 的
 * 发件人、金额勾稽、账户归属和交易一致性规则。
 *
 * @param input - 已受大小限制的原始 MIME 字节及不含路径的来源名称。
 * @returns 已验证 Schwab 来源、稳定消息标识及正文候选。
 * @throws IngestError 当 MIME、身份头或正文不符合 eConfirm 输入要求时抛出。
 */
export async function parseRfcMimeMessage(
  input: ParseRfcMimeMessageInput,
): Promise<GmailMessage> {
  const sourceName = requireSourceName(input.sourceName);
  if (input.content.byteLength === 0) {
    throw new IngestError(`${sourceName}: MIME 邮件不能为空`);
  }

  let parsed: ParsedMail;
  try {
    parsed = await simpleParser(Buffer.from(input.content), {
      keepCidLinks: true,
      skipHtmlToText: true,
      skipTextLinks: true,
      skipTextToHtml: true,
    });
  } catch (error: unknown) {
    throw new IngestError(`${sourceName}: 无法解析原始 RFC MIME 邮件`);
  }

  const sender = parsed.from?.text.trim() ?? "";
  const subject = parsed.subject?.trim() ?? "";
  if (!SCHWAB_SENDER_PATTERN.test(sender)) {
    throw new IngestError(
      `${sourceName}: 发件人不是可识别的 Schwab eConfirm 地址: ${JSON.stringify(sender)}`,
    );
  }
  if (!subject.includes(SCHWAB_ECONFIRM_SUBJECT)) {
    throw new IngestError(
      `${sourceName}: 不是 Schwab eConfirms 邮件: ${JSON.stringify(subject)}`,
    );
  }

  const bodyParts: [string, string][] = [];
  if (typeof parsed.text === "string") {
    bodyParts.push(["text/plain", parsed.text]);
  }
  if (typeof parsed.html === "string") {
    bodyParts.push(["text/html", parsed.html]);
  }
  const candidates = normalizeEmailBodyCandidates(bodyParts);
  const [body, ...bodyCandidates] = candidates;
  if (body === undefined) {
    throw new IngestError(
      `${sourceName}: MIME 中没有内联 text/plain 或 text/html 正文`,
    );
  }

  return {
    messageId: resolveMessageId(parsed, input.content, sourceName),
    sender,
    subject,
    body,
    emailTimestamp: resolveTimestamp(parsed, sourceName),
    bodyCandidates,
  };
}

/** 按 Gmail 扩展头、Message-ID、原文摘要的顺序生成幂等消息标识。 */
function resolveMessageId(
  parsed: ParsedMail,
  content: Uint8Array,
  sourceName: string,
): string {
  const identifier = readIdentifierHeader(parsed.headers.get("x-gm-msgid"), sourceName)
    ?? readIdentifierHeader(parsed.headers.get("x-gmail-message-id"), sourceName)
    ?? normalizeIdentifier(parsed.messageId, sourceName);
  return identifier ?? createHash("sha256").update(content).digest("hex");
}

/** 自定义消息标识头只允许唯一字符串值，避免重复头产生不明确身份。 */
function readIdentifierHeader(value: HeaderValue | undefined, sourceName: string): string | null {
  if (value === undefined) {
    return null;
  }
  if (typeof value !== "string") {
    throw new IngestError(`${sourceName}: MIME 消息标识头必须是唯一字符串`);
  }
  return normalizeIdentifier(value, sourceName);
}

/** 去除标准尖括号并拒绝空值或折行注入。 */
function normalizeIdentifier(value: string | undefined, sourceName: string): string | null {
  if (value === undefined) {
    return null;
  }
  const trimmed = value.trim();
  const normalized = trimmed.startsWith("<") && trimmed.endsWith(">")
    ? trimmed.slice(1, -1).trim()
    : trimmed;
  if (normalized.length === 0 || /[\r\n]/u.test(normalized)) {
    throw new IngestError(`${sourceName}: MIME 消息标识无效`);
  }
  return normalized;
}

/** 把 mailparser 的 Date 对象规范为可持久化 UTC 时间。 */
function resolveTimestamp(parsed: ParsedMail, sourceName: string): string | null {
  if (parsed.date === undefined) {
    return null;
  }
  if (Number.isNaN(parsed.date.getTime())) {
    throw new IngestError(`${sourceName}: MIME Date 头无效`);
  }
  return parsed.date.toISOString();
}

/** 来源名只用于安全错误定位，禁止路径或控制字符进入客户端消息。 */
function requireSourceName(value: string): string {
  const sourceName = value.trim();
  if (
    sourceName.length === 0
    || sourceName === "."
    || sourceName === ".."
    || /[/\\\r\n]/u.test(sourceName)
  ) {
    throw new IngestError("MIME 来源名称无效");
  }
  return sourceName;
}
