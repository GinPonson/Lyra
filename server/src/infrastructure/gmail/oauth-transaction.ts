import { createHash, randomBytes, timingSafeEqual } from "node:crypto";

/** Lyra Gmail 导入所需的唯一 Google OAuth scope。 */
export const GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly";

/** Google 授权事务与 scope 校验的稳定错误码。 */
export type GmailOAuthErrorCode =
  | "GOOGLE_AUTH_TRANSACTION_INVALID"
  | "GOOGLE_AUTH_TRANSACTION_EXPIRED"
  | "GOOGLE_AUTH_TRANSACTION_USED"
  | "GOOGLE_TOKEN_EXCHANGE_FAILED"
  | "GOOGLE_IDENTITY_UNVERIFIED"
  | "GMAIL_SCOPE_MISSING";

/** OAuth 错误可选的安全控制信息；cause 仅供服务端诊断。 */
export interface GmailOAuthErrorOptions {
  retryable?: boolean;
  cause?: unknown;
}

/** Google 授权事务或实际授权范围无效。 */
export class GmailOAuthError extends Error {
  readonly retryable: boolean;

  /** @param code - Android App 可稳定分支处理的错误码。 */
  constructor(
    readonly code: GmailOAuthErrorCode,
    message: string,
    options: GmailOAuthErrorOptions = {},
  ) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    this.name = "GmailOAuthError";
    this.retryable = options.retryable ?? false;
  }
}

/** 仅存在于服务端短期内存中的一次性 Google 授权事务。 */
export interface GoogleOAuthTransactionRecord {
  stateHash: string;
  /** 交换授权码时使用；不得返回 App、写入日志或长期持久化。 */
  codeVerifier: string;
  deviceId: string;
  expiresAt: string;
  consumedAt: string | null;
}

/** 创建授权事务时返回给 App 的公开参数与服务端私有记录。 */
export interface CreatedGoogleOAuthTransaction {
  state: string;
  codeChallenge: string;
  codeChallengeMethod: "S256";
  record: GoogleOAuthTransactionRecord;
}

/**
 * 创建绑定设备、短时且一次性的 Google OAuth state/PKCE 事务。
 *
 * @param deviceId - 发起授权的已认证设备 ID。
 * @param now - 服务端可信时钟。
 * @param ttlMilliseconds - 授权事务有效期毫秒数。
 */
export function createGoogleOAuthTransaction(
  deviceId: string,
  now: Date,
  ttlMilliseconds: number,
): CreatedGoogleOAuthTransaction {
  if (deviceId.trim().length === 0) {
    throw new RangeError("Google 授权事务 deviceId 不能为空");
  }
  if (!Number.isSafeInteger(ttlMilliseconds) || ttlMilliseconds <= 0) {
    throw new RangeError("Google 授权事务有效期必须是正安全整数毫秒数");
  }
  const state = randomBytes(32).toString("base64url");
  const codeVerifier = randomBytes(32).toString("base64url");
  return {
    state,
    codeChallenge: createHash("sha256").update(codeVerifier, "ascii").digest("base64url"),
    codeChallengeMethod: "S256",
    record: {
      stateHash: hashState(state),
      codeVerifier,
      deviceId,
      expiresAt: new Date(now.getTime() + ttlMilliseconds).toISOString(),
      consumedAt: null,
    },
  };
}

/**
 * 校验设备、state 和过期时间后一次性消费 Google 授权事务。
 *
 * 返回的新记录必须与授权码交换的开始状态原子保存；同一 state 不能重放。
 */
export function consumeGoogleOAuthTransaction(
  record: GoogleOAuthTransactionRecord,
  state: string,
  deviceId: string,
  now: Date,
): GoogleOAuthTransactionRecord {
  if (record.consumedAt !== null) {
    throw new GmailOAuthError(
      "GOOGLE_AUTH_TRANSACTION_USED",
      "Google 授权事务已经使用",
    );
  }
  if (now.getTime() >= Date.parse(record.expiresAt)) {
    throw new GmailOAuthError(
      "GOOGLE_AUTH_TRANSACTION_EXPIRED",
      "Google 授权事务已经过期",
    );
  }
  const candidateHash = Buffer.from(hashState(state), "hex");
  const expectedHash = Buffer.from(record.stateHash, "hex");
  if (
    deviceId !== record.deviceId
    || candidateHash.byteLength !== expectedHash.byteLength
    || !timingSafeEqual(candidateHash, expectedHash)
  ) {
    throw new GmailOAuthError(
      "GOOGLE_AUTH_TRANSACTION_INVALID",
      "Google 授权事务无效",
    );
  }
  return { ...record, consumedAt: now.toISOString() };
}

/**
 * 验证 Google token 响应实际授予了 Gmail 只读 scope。
 *
 * @param grantedScopeText - Google 返回的空格分隔 scope 字符串。
 * @returns 去重并排序后的实际 scope，适合写入连接元数据。
 */
export function assertGmailReadonlyScope(grantedScopeText: string): readonly string[] {
  const scopes = [...new Set(grantedScopeText.split(/\s+/).filter(Boolean))].toSorted();
  if (!scopes.includes(GMAIL_READONLY_SCOPE)) {
    throw new GmailOAuthError(
      "GMAIL_SCOPE_MISSING",
      "Google 授权未包含 Gmail 只读权限",
    );
  }
  return scopes;
}

/** 对高熵 OAuth state 生成仅服务端保存的 SHA-256 摘要。 */
function hashState(state: string): string {
  return createHash("sha256").update(state, "ascii").digest("hex");
}
