import type { LocalDate } from "../ledger/index.js";

/** Gmail 连接与增量游标相关的持久化状态。 */
export type GmailSyncStatus =
  | "pending_initial_scan"
  | "active"
  | "needs_resync"
  | "disconnected"
  | "error";

/** Gmail 同步状态机可由客户端稳定处理的预期错误码。 */
export const GMAIL_SYNC_ERROR_CODES = [
  "GMAIL_HISTORY_EXPIRED",
  "GMAIL_INCREMENTAL_LIMIT_EXCEEDED",
  "GMAIL_RESYNC_CONFIRMATION_REQUIRED",
  "GMAIL_AUTH_REVOKED",
  "GMAIL_CONNECTION_ERROR",
] as const;

/** Gmail 同步状态机可由客户端稳定处理的预期错误码。 */
export type GmailSyncErrorCode = typeof GMAIL_SYNC_ERROR_CODES[number];

/** Gmail 同步错误的传输语义；cause 只用于服务端诊断。 */
export interface GmailSyncErrorOptions {
  retryable?: boolean;
  cause?: unknown;
}

/** Gmail 连接当前状态无法执行同步。 */
export class GmailSyncError extends Error {
  readonly retryable: boolean;

  /** @param code - 客户端分支处理使用的稳定错误码。 */
  constructor(
    readonly code: GmailSyncErrorCode,
    message: string,
    options: GmailSyncErrorOptions = {},
  ) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    this.name = "GmailSyncError";
    this.retryable = options.retryable ?? false;
  }
}

/** 不包含令牌或邮箱正文的同步状态快照。 */
export interface GmailSyncState {
  status: GmailSyncStatus;
  historyId: string | null;
  lastSyncAt: string | null;
  lastErrorCode: GmailSyncErrorCode | null;
}

/** history REST 完成后交给状态机的最小观察值，不携带邮件正文。 */
export interface GmailIncrementalFetchObservation {
  observedHistoryId: string;
  candidateMessageCount: number;
  truncated: boolean;
}

/** 增量结果是否允许进入消息持久化及最终游标推进。 */
export type GmailIncrementalFetchDecision =
  | {
      kind: "ready";
      completionHistoryId: string;
      candidateMessageCount: number;
    }
  | {
      kind: "needs_resync";
      state: GmailSyncState;
      errorCode: "GMAIL_INCREMENTAL_LIMIT_EXCEEDED";
    };

/** 下一次 Gmail 查询只能是增量或经过用户确认的有界重扫。 */
export type GmailSyncRequest =
  | { mode: "incremental"; startHistoryId: string }
  | { mode: "bounded_rescan"; afterDate: LocalDate };

/**
 * 根据连接状态决定下一次 Gmail 查询方式。
 *
 * @param state - 当前持久化同步状态。
 * @param confirmedRescanAfterDate - 用户明确确认的首次扫描或重扫起始日期。
 * @returns 增量 history 请求或有界消息扫描请求。
 * @throws GmailSyncError 当连接不可同步，或需要重扫但用户尚未确认范围时抛出。
 */
export function planGmailSync(
  state: GmailSyncState,
  confirmedRescanAfterDate?: LocalDate,
): GmailSyncRequest {
  if (state.status === "active") {
    if (state.historyId === null) {
      throw new GmailSyncError(
        "GMAIL_HISTORY_EXPIRED",
        "Gmail 增量游标不可用，必须确认重扫范围",
      );
    }
    return { mode: "incremental", startHistoryId: normalizeGmailHistoryId(state.historyId) };
  }
  if (state.status === "pending_initial_scan" || state.status === "needs_resync") {
    if (confirmedRescanAfterDate === undefined) {
      throw new GmailSyncError(
        "GMAIL_RESYNC_CONFIRMATION_REQUIRED",
        "Gmail 首次扫描或重扫必须由用户确认起始日期",
      );
    }
    return { mode: "bounded_rescan", afterDate: confirmedRescanAfterDate };
  }
  if (state.status === "disconnected") {
    throw new GmailSyncError("GMAIL_AUTH_REVOKED", "Gmail 连接已断开，需要重新授权");
  }
  throw new GmailSyncError(
    "GMAIL_CONNECTION_ERROR",
    "Gmail 连接处于错误状态，必须先处理连接错误",
  );
}

/**
 * 在处理消息前决定 history 拉取结果是否允许继续。
 *
 * truncated 说明任务硬上限已阻止完整读取；此时保留旧游标、进入
 * needs_resync，且返回值中刻意不提供 completionHistoryId，避免误推进。
 */
export function decideGmailIncrementalFetch(
  state: GmailSyncState,
  observation: GmailIncrementalFetchObservation,
): GmailIncrementalFetchDecision {
  if (state.status !== "active" || state.historyId === null) {
    throw new GmailSyncError(
      "GMAIL_CONNECTION_ERROR",
      "只有具有有效游标的 active Gmail 连接可以处理增量结果",
    );
  }
  const observedHistoryId = normalizeGmailHistoryId(observation.observedHistoryId);
  if (
    !Number.isSafeInteger(observation.candidateMessageCount)
    || observation.candidateMessageCount < 0
  ) {
    throw new RangeError("Gmail 增量候选消息数必须是非负安全整数");
  }
  if (!observation.truncated) {
    return {
      kind: "ready",
      completionHistoryId: observedHistoryId,
      candidateMessageCount: observation.candidateMessageCount,
    };
  }

  const errorCode = "GMAIL_INCREMENTAL_LIMIT_EXCEEDED" as const;
  return {
    kind: "needs_resync",
    errorCode,
    state: {
      ...state,
      status: "needs_resync",
      lastErrorCode: errorCode,
    },
  };
}

/**
 * 在 Gmail 明确报告 history 游标失效时进入待确认重扫状态。
 *
 * 旧游标保留用于审计，但后续 `planGmailSync` 不会再使用它发起增量请求。
 */
export function markGmailHistoryExpired(
  state: GmailSyncState,
  errorCode: GmailSyncErrorCode,
): GmailSyncState {
  if (state.status !== "active") {
    throw new Error(`只有 active Gmail 连接可以标记 history 失效，当前为 ${state.status}`);
  }
  if (errorCode !== "GMAIL_HISTORY_EXPIRED") {
    throw new RangeError("Gmail history 失效只能记录稳定错误码 GMAIL_HISTORY_EXPIRED");
  }
  return {
    ...state,
    status: "needs_resync",
    lastErrorCode: errorCode,
  };
}

/** 判断数据库读取值是否属于可持久化、可公开的 Gmail 同步错误码。 */
export function isGmailSyncErrorCode(value: string): value is GmailSyncErrorCode {
  return (GMAIL_SYNC_ERROR_CODES as readonly string[]).includes(value);
}

/**
 * 在增量同步或用户确认的有界重扫全部成功后推进 history 游标。
 *
 * @param state - 同步开始时的连接状态。
 * @param newHistoryId - Gmail 成功响应返回的最新 history id。
 * @param syncedAt - 本次同步完成的 UTC 时间戳。
 * @returns 恢复 active 且清除旧错误的状态。
 */
export function completeGmailSync(
  state: GmailSyncState,
  newHistoryId: string,
  syncedAt: string,
): GmailSyncState {
  if (state.status === "disconnected") {
    throw new GmailSyncError(
      "GMAIL_AUTH_REVOKED",
      "已断开的 Gmail 连接不能完成同步，需要重新授权",
    );
  }
  const historyId = normalizeGmailHistoryId(newHistoryId);
  const completedAt = normalizeGmailSyncTimestamp(syncedAt);
  return {
    status: "active",
    historyId,
    lastSyncAt: completedAt,
    lastErrorCode: null,
  };
}

/** Gmail history ID 是不透明数值字符串，不能转换为可能溢出的 Number。 */
export function normalizeGmailHistoryId(value: string): string {
  const normalized = value.trim();
  if (!/^[1-9][0-9]*$/.test(normalized)) {
    throw new RangeError("Gmail history id 必须是正十进制字符串");
  }
  return normalized;
}

/** 规范 Gmail 同步完成时间，并拒绝无 UTC 标记或不可解析的输入。 */
export function normalizeGmailSyncTimestamp(value: string): string {
  const normalized = value.trim();
  if (!normalized.endsWith("Z")) {
    throw new RangeError("Gmail 同步完成时间必须是 UTC ISO 时间戳");
  }
  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) {
    throw new RangeError("Gmail 同步完成时间必须是 UTC ISO 时间戳");
  }
  return parsed.toISOString();
}
