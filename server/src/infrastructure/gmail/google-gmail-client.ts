import {
  GmailSyncError,
  SCHWAB_ECONFIRM_SUBJECT,
  SCHWAB_SENDER_PATTERN,
  normalizeGmailHistoryId,
} from "../../domain/gmail/index.js";
import type { GmailBoundedSearchPlan } from "./search-query.js";

/** Gmail REST messages 资源；用户固定为 access token 对应的 `me`。 */
export const GMAIL_MESSAGES_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/messages";

/** Gmail REST history 增量资源。 */
export const GMAIL_HISTORY_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/history";

/** Gmail REST 当前用户资料资源，用于首次扫描前固定 history 基线。 */
export const GMAIL_PROFILE_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/profile";

/** Gmail messages.list 单页允许的官方最大结果数。 */
const GMAIL_MAX_PAGE_SIZE = 500;

/** Gmail 单次 HTTP 调用超时；任务总量另由 maximumMessages 控制。 */
const GMAIL_REQUEST_TIMEOUT_MILLISECONDS = 15_000;

/** 仅这两类正文 part 允许通过 attachment API 补取内容。 */
const GMAIL_TEXT_MIME_TYPES = new Set(["text/plain", "text/html"]);

/** 为测试替换网络边界的最小 fetch 契约。 */
export type GmailApiFetch = (
  input: string | URL,
  init?: RequestInit,
) => Promise<Response>;

/** 有界扫描取得的 full 响应；后续交给既有确定性 MIME/eConfirm 解析层。 */
export interface BoundedGmailFullRecords {
  records: readonly Readonly<Record<string, unknown>>[];
  /** true 表示达到用户确认的硬上限时 Gmail 仍报告存在下一页。 */
  truncated: boolean;
}

/** history 增量扫描的输入；maximumMessages 是单任务硬上限。 */
export interface GmailIncrementalHistoryInput {
  startHistoryId: string;
  maximumMessages: number;
}

/** 增量扫描结果；truncated 时 observedHistoryId 只能用于诊断，不得推进游标。 */
export interface IncrementalGmailFullRecords {
  records: readonly Readonly<Record<string, unknown>>[];
  ignoredMessageCount: number;
  observedHistoryId: string;
  truncated: boolean;
}

/** 已校验与连接邮箱一致的 Gmail profile。 */
export interface GmailProfile {
  emailAddress: string;
  historyId: string;
  messagesTotal: number;
  threadsTotal: number;
}

/** 首次扫描只有完整覆盖硬上限内结果时才提供可完成的 history 游标。 */
export type InitialGmailFullRecords =
  | {
      kind: "ready";
      records: readonly Readonly<Record<string, unknown>>[];
      completionHistoryId: string;
    }
  | {
      kind: "truncated";
      records: readonly Readonly<Record<string, unknown>>[];
    };

/**
 * 按有界查询分页列出 message id，再逐封取得 `format=full` 标准响应。
 *
 * access token 只放在 Authorization header；查询总数严格受
 * `plan.maximumMessages` 限制，不因 Gmail `nextPageToken` 自动扩张。
 *
 * @param accessToken - 内存中的短期 Google access token。
 * @param plan - 已固定 Schwab 来源、起始日和任务硬上限的查询计划。
 * @param fetchFn - 默认使用 Node 原生 fetch；测试可注入 HTTP 边界。
 */
export async function fetchBoundedGmailFullRecords(
  accessToken: string,
  plan: GmailBoundedSearchPlan,
  fetchFn: GmailApiFetch = globalThis.fetch,
): Promise<BoundedGmailFullRecords> {
  const token = requireText(accessToken, "Google access token");
  const query = requireText(plan.query, "Gmail 查询");
  if (!Number.isSafeInteger(plan.maximumMessages) || plan.maximumMessages <= 0) {
    throw new RangeError("Gmail 有界扫描上限必须是正安全整数");
  }

  const messageIds: string[] = [];
  const seenMessageIds = new Set<string>();
  const seenPageTokens = new Set<string>();
  let pageToken: string | undefined;
  let truncated = false;

  do {
    const remaining = plan.maximumMessages - messageIds.length;
    const url = new URL(GMAIL_MESSAGES_ENDPOINT);
    url.searchParams.set("q", query);
    url.searchParams.set("maxResults", String(Math.min(GMAIL_MAX_PAGE_SIZE, remaining)));
    if (pageToken !== undefined) {
      url.searchParams.set("pageToken", pageToken);
    }

    const page = await requestJsonObject(url, token, fetchFn);
    const pageIds = parseMessageIds(page.messages);
    for (const messageId of pageIds) {
      if (seenMessageIds.has(messageId)) {
        throw invalidGmailResponse("messages.list 返回重复 message id");
      }
      seenMessageIds.add(messageId);
      messageIds.push(messageId);
      if (messageIds.length === plan.maximumMessages) {
        break;
      }
    }

    const nextPageToken = optionalText(page.nextPageToken, "nextPageToken");
    if (messageIds.length === plan.maximumMessages) {
      truncated = nextPageToken !== undefined || pageIds.length > remaining;
      break;
    }
    if (nextPageToken !== undefined) {
      if (seenPageTokens.has(nextPageToken)) {
        throw invalidGmailResponse("messages.list 返回重复 page token");
      }
      seenPageTokens.add(nextPageToken);
    }
    pageToken = nextPageToken;
  } while (pageToken !== undefined);

  return {
    records: await fetchFullRecords(messageIds, token, fetchFn),
    truncated,
  };
}

/**
 * 在首次有界扫描前固定 Gmail profile/history 基线。
 *
 * profile 必须先于 messages.list 请求，确保扫描期间新增的消息仍可由后续
 * history 增量覆盖。邮箱与授权时已验证身份不一致时立即停止。
 */
export async function fetchInitialGmailFullRecords(
  accessToken: string,
  expectedGoogleEmail: string,
  plan: GmailBoundedSearchPlan,
  fetchFn: GmailApiFetch = globalThis.fetch,
): Promise<InitialGmailFullRecords> {
  const profile = await fetchGmailProfile(
    accessToken,
    expectedGoogleEmail,
    fetchFn,
  );
  const bounded = await fetchBoundedGmailFullRecords(accessToken, plan, fetchFn);
  if (bounded.truncated) {
    return { kind: "truncated", records: bounded.records };
  }
  return {
    kind: "ready",
    records: bounded.records,
    completionHistoryId: profile.historyId,
  };
}

/** 读取并严格校验 Gmail profile 与连接时已验证的邮箱身份。 */
export async function fetchGmailProfile(
  accessToken: string,
  expectedGoogleEmail: string,
  fetchFn: GmailApiFetch = globalThis.fetch,
): Promise<GmailProfile> {
  const token = requireText(accessToken, "Google access token");
  const expectedEmail = requireEmail(expectedGoogleEmail, "已验证 Google 邮箱");
  const profile = await requestJsonObject(new URL(GMAIL_PROFILE_ENDPOINT), token, fetchFn);
  const emailAddress = requireEmailField(profile.emailAddress, "Gmail profile 邮箱无效");
  if (emailAddress !== expectedEmail) {
    throw invalidGmailResponse("Gmail profile 与已连接的 Google 邮箱不一致");
  }
  return {
    emailAddress,
    historyId: requireHistoryId(profile.historyId, "Gmail profile history id"),
    messagesTotal: requireNonNegativeCount(profile.messagesTotal, "messagesTotal"),
    threadsTotal: requireNonNegativeCount(profile.threadsTotal, "threadsTotal"),
  };
}

/**
 * 从已持久化 history 游标读取新增消息，并取得对应 format=full 响应。
 *
 * 只请求 `messageAdded` 事件。达到硬上限时仍返回已取得的有界记录，但
 * `truncated=true`；调用方必须停止批次并进入需确认重扫状态，不能使用
 * observedHistoryId 完成同步。
 */
export async function fetchIncrementalGmailFullRecords(
  accessToken: string,
  input: GmailIncrementalHistoryInput,
  fetchFn: GmailApiFetch = globalThis.fetch,
): Promise<IncrementalGmailFullRecords> {
  const token = requireText(accessToken, "Google access token");
  const startHistoryId = requireInputHistoryId(input.startHistoryId);
  if (!Number.isSafeInteger(input.maximumMessages) || input.maximumMessages <= 0) {
    throw new RangeError("Gmail 增量消息上限必须是正安全整数");
  }

  const messageIds: string[] = [];
  const seenMessageIds = new Set<string>();
  const seenPageTokens = new Set<string>();
  let pageToken: string | undefined;
  let observedHistoryId: string | null = null;
  let truncated = false;

  do {
    const remaining = input.maximumMessages - messageIds.length;
    const url = new URL(GMAIL_HISTORY_ENDPOINT);
    url.searchParams.set("startHistoryId", startHistoryId);
    url.searchParams.set("historyTypes", "messageAdded");
    url.searchParams.set("maxResults", String(Math.min(GMAIL_MAX_PAGE_SIZE, remaining)));
    if (pageToken !== undefined) {
      url.searchParams.set("pageToken", pageToken);
    }

    const page = await requestJsonObject(url, token, fetchFn, true);
    observedHistoryId = requireHistoryId(page.historyId, "Gmail observed history id");
    const pageIds = parseAddedMessageIds(page.history);
    let exceededWithinPage = false;
    for (const messageId of pageIds) {
      if (seenMessageIds.has(messageId)) {
        continue;
      }
      if (messageIds.length === input.maximumMessages) {
        exceededWithinPage = true;
        break;
      }
      seenMessageIds.add(messageId);
      messageIds.push(messageId);
    }

    const nextPageToken = optionalText(page.nextPageToken, "nextPageToken");
    if (messageIds.length === input.maximumMessages) {
      truncated = exceededWithinPage || nextPageToken !== undefined;
      break;
    }
    if (nextPageToken !== undefined) {
      if (seenPageTokens.has(nextPageToken)) {
        throw invalidGmailResponse("history.list 返回重复 page token");
      }
      seenPageTokens.add(nextPageToken);
    }
    pageToken = nextPageToken;
  } while (pageToken !== undefined);

  if (observedHistoryId === null) {
    throw invalidGmailResponse("history.list 缺少 observed history id");
  }
  const relevant = await fetchRelevantFullRecords(messageIds, token, fetchFn);
  return {
    records: relevant.records,
    ignoredMessageCount: relevant.ignoredMessageCount,
    observedHistoryId,
    truncated,
  };
}

/**
 * history 无法使用 Gmail 搜索 query，因此先只读取 From/Subject 元数据。
 * 只有已确认 Schwab eConfirm 来源才继续下载 full 正文。
 */
async function fetchRelevantFullRecords(
  messageIds: readonly string[],
  accessToken: string,
  fetchFn: GmailApiFetch,
): Promise<{
  records: readonly Readonly<Record<string, unknown>>[];
  ignoredMessageCount: number;
}> {
  const relevantIds: string[] = [];
  for (const messageId of messageIds) {
    const url = new URL(`${GMAIL_MESSAGES_ENDPOINT}/${encodeURIComponent(messageId)}`);
    url.searchParams.set("format", "metadata");
    url.searchParams.append("metadataHeaders", "From");
    url.searchParams.append("metadataHeaders", "Subject");
    const metadata = await requestJsonObject(url, accessToken, fetchFn);
    if (metadata.id !== messageId) {
      throw invalidGmailResponse("messages.get metadata 响应 id 与请求不一致");
    }
    if (isSchwabEconfirmMetadata(metadata)) {
      relevantIds.push(messageId);
    }
  }
  return {
    records: await fetchFullRecords(relevantIds, accessToken, fetchFn),
    ignoredMessageCount: messageIds.length - relevantIds.length,
  };
}

/** 逐封获取 full 响应，并校验 Gmail 没有返回其他 message id。 */
async function fetchFullRecords(
  messageIds: readonly string[],
  accessToken: string,
  fetchFn: GmailApiFetch,
): Promise<readonly Readonly<Record<string, unknown>>[]> {
  const records: Readonly<Record<string, unknown>>[] = [];
  for (const messageId of messageIds) {
    const url = new URL(`${GMAIL_MESSAGES_ENDPOINT}/${encodeURIComponent(messageId)}`);
    url.searchParams.set("format", "full");
    const record = await requestJsonObject(url, accessToken, fetchFn);
    if (record.id !== messageId) {
      throw invalidGmailResponse("messages.get 响应 id 与请求不一致");
    }
    records.push(await hydrateGmailTextAttachments(record, messageId, accessToken, fetchFn));
  }
  return records;
}

/**
 * 递归补取 Gmail 作为 attachment 返回的文本正文 part。
 *
 * 只处理 text/plain 与 text/html；PDF、图片及其他附件保持原样且不发起请求。
 */
async function hydrateGmailTextAttachments(
  record: Readonly<Record<string, unknown>>,
  messageId: string,
  accessToken: string,
  fetchFn: GmailApiFetch,
): Promise<Readonly<Record<string, unknown>>> {
  if (!isRecord(record.payload)) {
    return record;
  }
  return {
    ...record,
    payload: await hydrateGmailPart(record.payload, messageId, accessToken, fetchFn),
  };
}

/** 复制 MIME part 并只替换需要补取的 body/parts，避免修改原始响应。 */
async function hydrateGmailPart(
  part: Readonly<Record<string, unknown>>,
  messageId: string,
  accessToken: string,
  fetchFn: GmailApiFetch,
): Promise<Readonly<Record<string, unknown>>> {
  let hydratedBody = part.body;
  const mimeType = typeof part.mimeType === "string" ? part.mimeType.toLowerCase() : "";
  if (GMAIL_TEXT_MIME_TYPES.has(mimeType) && isRecord(part.body)) {
    const hasInlineData = typeof part.body.data === "string" && part.body.data.length > 0;
    if (!hasInlineData && part.body.attachmentId !== undefined) {
      const attachmentId = requireAttachmentId(part.body.attachmentId);
      const url = new URL(
        `${GMAIL_MESSAGES_ENDPOINT}/${encodeURIComponent(messageId)}`
        + `/attachments/${encodeURIComponent(attachmentId)}`,
      );
      const attachment = await requestJsonObject(url, accessToken, fetchFn);
      const data = requireBase64UrlData(attachment.data);
      hydratedBody = { ...part.body, data };
    }
  }

  let hydratedParts = part.parts;
  if (part.parts !== undefined) {
    if (!Array.isArray(part.parts)) {
      throw invalidGmailResponse("Gmail MIME parts 必须是数组");
    }
    hydratedParts = await Promise.all(part.parts.map(async (child) => {
      if (!isRecord(child)) {
        throw invalidGmailResponse("Gmail MIME part 必须是对象");
      }
      return hydrateGmailPart(child, messageId, accessToken, fetchFn);
    }));
  }
  return {
    ...part,
    ...(hydratedBody === undefined ? {} : { body: hydratedBody }),
    ...(hydratedParts === undefined ? {} : { parts: hydratedParts }),
  };
}

/** 发起 Gmail JSON 请求，并统一处理鉴权失效、临时故障和不可信响应。 */
async function requestJsonObject(
  url: URL,
  accessToken: string,
  fetchFn: GmailApiFetch,
  historyNotFoundMeansExpired = false,
): Promise<Readonly<Record<string, unknown>>> {
  let response: Response;
  try {
    response = await fetchFn(url, {
      method: "GET",
      headers: { authorization: `Bearer ${accessToken}` },
      signal: AbortSignal.timeout(GMAIL_REQUEST_TIMEOUT_MILLISECONDS),
    });
  } catch (error) {
    throw new GmailSyncError(
      "GMAIL_CONNECTION_ERROR",
      "无法连接 Gmail，请稍后重试",
      { retryable: true, cause: error },
    );
  }
  if (!response.ok) {
    if (historyNotFoundMeansExpired && response.status === 404) {
      throw new GmailSyncError(
        "GMAIL_HISTORY_EXPIRED",
        "Gmail 增量游标已过期，必须确认重扫范围",
        { cause: new Error("Gmail history endpoint returned HTTP 404") },
      );
    }
    throw gmailHttpError(response.status);
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch (error) {
    throw invalidGmailResponse("Gmail 返回了非法 JSON", error);
  }
  if (!isRecord(payload)) {
    throw invalidGmailResponse("Gmail JSON 根节点不是对象");
  }
  return payload;
}

/** 从 history.list 的 messageAdded 事件中提取并校验 message id。 */
function parseAddedMessageIds(value: unknown): readonly string[] {
  if (value === undefined) {
    return [];
  }
  if (!Array.isArray(value)) {
    throw invalidGmailResponse("history.list 的 history 不是数组");
  }
  const messageIds: string[] = [];
  for (const history of value) {
    if (!isRecord(history)) {
      throw invalidGmailResponse("history.list 项不是对象");
    }
    if (history.messagesAdded === undefined) {
      continue;
    }
    if (!Array.isArray(history.messagesAdded)) {
      throw invalidGmailResponse("history.list 的 messagesAdded 不是数组");
    }
    for (const added of history.messagesAdded) {
      if (!isRecord(added) || !isRecord(added.message)) {
        throw invalidGmailResponse("history.list 的 messageAdded 项无效");
      }
      messageIds.push(parseMessageId(added.message.id, "messageAdded 缺少 message id"));
    }
  }
  return messageIds;
}

/** 从 metadata 响应中识别已确认的 Schwab 发件人与 eConfirm 主题。 */
function isSchwabEconfirmMetadata(metadata: Readonly<Record<string, unknown>>): boolean {
  if (!isRecord(metadata.payload) || !Array.isArray(metadata.payload.headers)) {
    throw invalidGmailResponse("messages.get metadata 缺少 payload.headers");
  }
  const headers = new Map<string, string>();
  for (const header of metadata.payload.headers) {
    if (!isRecord(header) || typeof header.name !== "string" || typeof header.value !== "string") {
      throw invalidGmailResponse("messages.get metadata header 无效");
    }
    headers.set(header.name.toLowerCase(), header.value);
  }
  const sender = headers.get("from") ?? "";
  const subject = headers.get("subject") ?? "";
  return SCHWAB_SENDER_PATTERN.test(sender) && subject.includes(SCHWAB_ECONFIRM_SUBJECT);
}

/** 严格提取 messages.list id，不接受缺失或路径式标识。 */
function parseMessageIds(value: unknown): readonly string[] {
  if (value === undefined) {
    return [];
  }
  if (!Array.isArray(value)) {
    throw invalidGmailResponse("messages.list 的 messages 不是数组");
  }
  return value.map((item) => {
    if (!isRecord(item)) {
      throw invalidGmailResponse("messages.list 项不是对象");
    }
    return parseMessageId(item.id, "messages.list 缺少 message id");
  });
}

/** Gmail message id 只能作为一个受控路径段使用。 */
function parseMessageId(value: unknown, message: string): string {
  const id = requireTextField(value, message);
  if (!/^[A-Za-z0-9_-]{1,256}$/.test(id)) {
    throw invalidGmailResponse("Gmail 返回非法 message id");
  }
  return id;
}

/** 提取可选非空分页令牌，空字符串视为协议错误。 */
function optionalText(value: unknown, field: string): string | undefined {
  if (value === undefined) {
    return undefined;
  }
  if (typeof value !== "string" || value.trim().length === 0) {
    throw invalidGmailResponse(`Gmail ${field} 无效`);
  }
  return value.trim();
}

/** 把 Gmail HTTP 状态映射为稳定、可脱敏的同步错误。 */
function gmailHttpError(status: number): GmailSyncError {
  if (status === 401 || status === 403) {
    return new GmailSyncError(
      "GMAIL_AUTH_REVOKED",
      "Gmail 授权已失效，需要重新授权",
      { cause: new Error(`Gmail endpoint returned HTTP ${status}`) },
    );
  }
  return new GmailSyncError(
    "GMAIL_CONNECTION_ERROR",
    "Gmail 请求失败，请稍后重试",
    {
      retryable: status === 429 || status >= 500,
      cause: new Error(`Gmail endpoint returned HTTP ${status}`),
    },
  );
}

/** Gmail 成功响应违反约定时明确失败，不返回部分扫描结果。 */
function invalidGmailResponse(message: string, cause?: unknown): GmailSyncError {
  return new GmailSyncError(
    "GMAIL_CONNECTION_ERROR",
    message,
    { cause },
  );
}

/** 校验调用方提供的必需文本。 */
function requireText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}

/** 校验 Gmail JSON 中的必需文本字段。 */
function requireTextField(value: unknown, message: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw invalidGmailResponse(message);
  }
  return value.trim();
}

/** history id 是 Google 返回的十进制不透明游标。 */
function requireHistoryId(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw invalidGmailResponse(`${label} 无效`);
  }
  try {
    return normalizeGmailHistoryId(value);
  } catch (error) {
    throw invalidGmailResponse(`${label} 无效`, error);
  }
}

/** 调用方提供的起始游标无效属于参数错误，不伪装成 Gmail 上游故障。 */
function requireInputHistoryId(value: string): string {
  return normalizeGmailHistoryId(value);
}

/** 校验调用方持有的已验证邮箱。 */
function requireEmail(value: string, label: string): string {
  const normalized = value.trim().toLowerCase();
  if (!/^[^@\s]+@[^@\s]+$/.test(normalized)) {
    throw new RangeError(`${label}无效`);
  }
  return normalized;
}

/** 校验 Gmail profile 响应邮箱，不把原值写入错误。 */
function requireEmailField(value: unknown, message: string): string {
  if (typeof value !== "string") {
    throw invalidGmailResponse(message);
  }
  const normalized = value.trim().toLowerCase();
  if (!/^[^@\s]+@[^@\s]+$/.test(normalized)) {
    throw invalidGmailResponse(message);
  }
  return normalized;
}

/** Gmail profile 计数必须是非负安全整数。 */
function requireNonNegativeCount(value: unknown, field: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw invalidGmailResponse(`Gmail profile ${field} 无效`);
  }
  return value as number;
}

/** attachment id 只允许作为单一路径段，拒绝路径注入和异常超长值。 */
function requireAttachmentId(value: unknown): string {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{1,2048}$/.test(value)) {
    throw invalidGmailResponse("Gmail text attachment id 无效");
  }
  return value;
}

/** attachment API 必须返回合法的 Gmail Base64URL data。 */
function requireBase64UrlData(value: unknown): string {
  if (
    typeof value !== "string"
    || value.length === 0
    || !/^[A-Za-z0-9_-]+$/.test(value)
    || value.length % 4 === 1
  ) {
    throw invalidGmailResponse("Gmail text attachment data 无效");
  }
  return value;
}

/** 判断 unknown 是否为 JSON object。 */
function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
