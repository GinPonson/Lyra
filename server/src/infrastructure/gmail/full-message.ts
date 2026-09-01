import {
  IngestError,
} from "../../domain/ledger/index.js";
import {
  SCHWAB_ECONFIRM_SUBJECT,
  SCHWAB_SENDER_PATTERN,
} from "../../domain/gmail/index.js";
import type { GmailMessage } from "../../domain/gmail/index.js";

/** Gmail `format=full` 中允许直接解析的内联正文类型。 */
const TEXT_MIME_TYPES = new Set(["text/plain", "text/html"]);

/** HTML 转文本时需要形成字段边界的块级标签。 */
const BLOCK_TAGS = [
  "address", "article", "br", "div", "footer", "h1", "h2", "h3", "h4",
  "header", "li", "p", "section", "table", "td", "th", "tr", "ul",
].join("|");

/** Gmail full 响应中递归 MIME part 的最小可用结构。 */
interface GmailPart {
  mimeType: string;
  headers: readonly GmailHeader[];
  body: Readonly<Record<string, unknown>>;
  parts: readonly GmailPart[];
}

/** Gmail header 的标准 name/value 对。 */
interface GmailHeader {
  name: string;
  value: string;
}

/**
 * 解析一个或一组 Gmail `users.messages.get(format=full)` 标准响应。
 *
 * 本适配器只负责已经由 Gmail API 取得的 JSON，不发起网络请求，也不接受
 * `messages.list` 摘要、连接器自定义对象、`format=raw` 或附件式正文。
 *
 * @param input - JSON 反序列化后的单个 full 响应或响应数组。
 * @param sourceName - 错误定位使用的上传文件名或同步批次名。
 * @returns 已验证发件人、主题并提取正文候选的标准邮件数组。
 * @throws IngestError 当 Gmail 结构、编码、发件人或主题不符合约定时抛出。
 */
export function parseGmailFullInput(
  input: unknown,
  sourceName: string,
): readonly GmailMessage[] {
  if (isRecord(input) && "messages" in input) {
    throw new IngestError(
      `${sourceName}: Gmail users.messages.list 只有 message id；`
      + "必须继续调用 users.messages.get(format=full)",
    );
  }
  const records = Array.isArray(input) ? input : [input];
  if (records.length === 0) {
    throw new IngestError(`${sourceName}: Gmail JSON 中没有邮件`);
  }

  return records.map((record, index) => {
    const where = `${sourceName} 第${index + 1}封`;
    if (!isRecord(record)) {
      throw new IngestError(`${where}: 邮件必须是 JSON 对象`);
    }
    return parseFullRecord(record, where);
  });
}

/** 解析单个 Gmail full 记录并完成来源白名单校验。 */
function parseFullRecord(record: Readonly<Record<string, unknown>>, where: string): GmailMessage {
  const messageId = requiredString(record.id, `${where}: 缺少 Gmail message id`);
  if (!isRecord(record.payload)) {
    if (typeof record.raw === "string") {
      throw new IngestError(`${where}: format=raw 需要 RFC 5322 MIME 适配器解析`);
    }
    throw new IngestError(
      `${where}: 不是 Gmail users.messages.get(format=full) 标准响应`,
    );
  }

  const payload = parsePart(record.payload, `${where} payload`);
  const headers = headerMap(payload.headers);
  const sender = headers.get("from") ?? "";
  const subject = headers.get("subject") ?? "";
  if (!SCHWAB_SENDER_PATTERN.test(sender)) {
    throw new IngestError(`${where}: 发件人不是可识别的 Schwab 域名: ${JSON.stringify(sender)}`);
  }
  if (!subject.includes(SCHWAB_ECONFIRM_SUBJECT)) {
    throw new IngestError(`${where}: 不是 Schwab eConfirms 邮件: ${JSON.stringify(subject)}`);
  }

  const candidates = normalizeEmailBodyCandidates(collectTextParts(payload, where));
  if (candidates.length === 0) {
    throw new IngestError(
      `${where}: Gmail format=full 不含内联 text/plain 或 text/html 正文；`
      + "附件式正文必须先通过 Gmail API 取得 attachment data",
    );
  }
  const [body, ...bodyCandidates] = candidates;
  if (body === undefined) {
    throw new IngestError(`${where}: Gmail format=full 没有可解析的文本正文`);
  }

  const dateHeader = headers.get("date");
  const internalDate = typeof record.internalDate === "string"
    ? record.internalDate
    : typeof record.internalDate === "number"
      ? String(record.internalDate)
      : null;
  return {
    messageId,
    sender,
    subject,
    body,
    emailTimestamp: dateHeader === undefined || dateHeader.length === 0
      ? internalDate
      : dateHeader,
    bodyCandidates,
  };
}

/** 把 unknown MIME part 收窄为本适配器使用的严格结构。 */
function parsePart(value: Readonly<Record<string, unknown>>, where: string): GmailPart {
  const mimeType = typeof value.mimeType === "string" ? value.mimeType.toLowerCase() : "";
  if (mimeType.length === 0) {
    throw new IngestError(`${where}: Gmail MIME part 缺少 mimeType`);
  }
  const headers = parseHeaders(value.headers, where);
  const body = isRecord(value.body) ? value.body : {};
  const rawParts = value.parts;
  if (rawParts !== undefined && !Array.isArray(rawParts)) {
    throw new IngestError(`${where}: Gmail MIME parts 必须是数组`);
  }
  const parts = (rawParts ?? []).map((child, index) => {
    if (!isRecord(child)) {
      throw new IngestError(`${where}: Gmail MIME part ${index + 1} 必须是对象`);
    }
    return parsePart(child, `${where} part ${index + 1}`);
  });
  return { mimeType, headers, body, parts };
}

/** 严格解析 Gmail headers 数组，避免对象或标量被静默字符串化。 */
function parseHeaders(value: unknown, where: string): readonly GmailHeader[] {
  if (!Array.isArray(value)) {
    throw new IngestError(`${where}: Gmail payload 缺少标准 headers 数组`);
  }
  return value.map((header, index) => {
    if (!isRecord(header) || typeof header.name !== "string" || typeof header.value !== "string") {
      throw new IngestError(`${where}: Gmail header ${index + 1} 必须包含字符串 name/value`);
    }
    return { name: header.name, value: header.value };
  });
}

/** 递归收集所有包含内联 data 的 plain/HTML part。 */
function collectTextParts(part: GmailPart, where: string): readonly [string, string][] {
  const results: [string, string][] = [];
  if (TEXT_MIME_TYPES.has(part.mimeType)) {
    const data = part.body.data;
    if (typeof data === "string" && data.length > 0) {
      const contentType = part.headers.find((header) => (
        header.name.toLowerCase() === "content-type"
      ))?.value ?? "";
      const charset = /charset=["']?([^;\s"']+)/i.exec(contentType)?.[1] ?? "utf-8";
      const bytes = decodeBase64Url(data, where);
      try {
        const content = new TextDecoder(charset, { fatal: true }).decode(bytes);
        results.push([part.mimeType, content]);
      } catch (error: unknown) {
        if (error instanceof RangeError || error instanceof TypeError) {
          throw new IngestError(`${where}: 无法按 charset=${JSON.stringify(charset)} 解码邮件正文`);
        }
        throw error;
      }
    }
  }
  for (const child of part.parts) {
    results.push(...collectTextParts(child, where));
  }
  return results;
}

/** plain 优先、HTML 次之，并对完全相同的候选去重。 */
export function normalizeEmailBodyCandidates(
  parts: readonly [string, string][],
): readonly string[] {
  const candidates: string[] = [];
  for (const expectedType of ["text/plain", "text/html"] as const) {
    for (const [mimeType, content] of parts) {
      if (mimeType !== expectedType) {
        continue;
      }
      const candidate = mimeType === "text/html" ? htmlToText(content) : content;
      if (candidate.trim().length > 0 && !candidates.includes(candidate)) {
        candidates.push(candidate);
      }
    }
  }
  return candidates;
}

/**
 * 把受信邮件 HTML 转为保留字段边界的文本。
 *
 * 这不是通用浏览器渲染器；它仅删除不可见脚本/样式、在已确认的块标签处换行，
 * 并解码常见字符实体，满足 eConfirm 确定性解析器的结构要求。
 */
function htmlToText(html: string): string {
  const withoutInvisible = html
    .replace(/<script\b[^>]*>[\s\S]*?<\/script\s*>/gi, "")
    .replace(/<style\b[^>]*>[\s\S]*?<\/style\s*>/gi, "");
  const withBoundaries = withoutInvisible.replace(
    new RegExp(`<\\/?(?:${BLOCK_TAGS})\\b[^>]*>`, "gi"),
    "\n",
  );
  const visible = decodeHtmlEntities(withBoundaries.replace(/<[^>]*>/g, ""));
  const lines = visible.replace(/\r/g, "").split("\n")
    .map((line) => line.replace(/[ \t\f\v]+/g, " ").trim())
    .filter((line) => line.length > 0);
  return lines.join("\n\n");
}

/** 解码邮件正文中 eConfirm 字段可能使用的标准 HTML 实体。 */
function decodeHtmlEntities(value: string): string {
  const named = new Map([
    ["amp", "&"], ["lt", "<"], ["gt", ">"], ["quot", "\""],
    ["apos", "'"], ["nbsp", " "],
  ]);
  return value.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (entity, token: string) => {
    if (token.startsWith("#x") || token.startsWith("#X")) {
      return String.fromCodePoint(Number.parseInt(token.slice(2), 16));
    }
    if (token.startsWith("#")) {
      return String.fromCodePoint(Number.parseInt(token.slice(1), 10));
    }
    return named.get(token.toLowerCase()) ?? entity;
  });
}

/** 严格解码 Gmail 使用的无填充 Base64URL。 */
function decodeBase64Url(value: string, where: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]*$/.test(value) || value.length % 4 === 1) {
    throw new IngestError(`${where}: Gmail Base64URL 数据无效`);
  }
  return Buffer.from(value, "base64url");
}

/** 把 headers 数组转换为不区分大小写的最后值映射。 */
function headerMap(headers: readonly GmailHeader[]): ReadonlyMap<string, string> {
  return new Map(headers.map((header) => [header.name.toLowerCase(), header.value]));
}

/** 读取必填非空字符串。 */
function requiredString(value: unknown, message: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new IngestError(message);
  }
  return value.trim();
}

/** 判断 unknown 是否为非空原型无关的普通记录。 */
function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
