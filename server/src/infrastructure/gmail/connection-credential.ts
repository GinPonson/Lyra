import type {
  GmailSyncErrorCode,
  GmailSyncStatus,
} from "../../domain/gmail/index.js";
import { SecretCipher } from "../security/index.js";
import { assertGmailReadonlyScope } from "./oauth-transaction.js";

/** 已由 Google 官方客户端完成签名、受众和发行方校验的身份。 */
export interface VerifiedGoogleIdentity {
  subject: string;
  email: string;
  emailVerified: boolean;
}

/** Google 授权码交换后建立离线连接所需的最小授权结果。 */
export interface GoogleOfflineGrant {
  refreshToken: string;
  grantedScopeText: string;
}

/** 与 gmail_connections 表对齐且不包含明文令牌的连接记录。 */
export interface GmailConnectionRecord {
  id: string;
  googleSubject: string;
  googleEmail: string;
  encryptedRefreshToken: string | null;
  grantedScopes: string;
  historyId: string | null;
  status: GmailSyncStatus;
  lastErrorCode: GmailSyncErrorCode | null;
  lastSyncAt: string | null;
  createdAt: string;
  updatedAt: string;
}

/**
 * 把经过官方库验证的 Google 身份与离线授权结果转换为安全连接记录。
 *
 * access token 不属于本函数输入，也不会持久化。refresh token 使用连接 ID
 * 作为 AES-GCM AAD 加密；实际 scope 缺少 Gmail readonly 时明确失败。
 */
export function createGmailConnectionRecord(input: {
  connectionId: string;
  identity: VerifiedGoogleIdentity;
  grant: GoogleOfflineGrant;
  cipher: SecretCipher;
  now: Date;
}): GmailConnectionRecord {
  const id = requireText(input.connectionId, "Gmail 连接 ID");
  const googleSubject = requireText(input.identity.subject, "Google subject");
  const googleEmail = requireText(input.identity.email, "Google 邮箱").toLowerCase();
  if (!input.identity.emailVerified || !/^[^@\s]+@[^@\s]+$/.test(googleEmail)) {
    throw new RangeError("Google 邮箱身份尚未验证或格式无效");
  }
  const refreshToken = requireText(input.grant.refreshToken, "Google refresh token");
  const scopes = assertGmailReadonlyScope(input.grant.grantedScopeText);
  const timestamp = requireTimestamp(input.now);

  return {
    id,
    googleSubject,
    googleEmail,
    encryptedRefreshToken: input.cipher.encrypt(
      refreshToken,
      `gmail-connection:${id}`,
    ),
    grantedScopes: scopes.join(" "),
    historyId: null,
    status: "pending_initial_scan",
    lastErrorCode: null,
    lastSyncAt: null,
    createdAt: timestamp,
    updatedAt: timestamp,
  };
}

/**
 * 断开 Gmail 并清除可继续访问邮箱的 refresh token 密文。
 * 历史游标和既有消息元数据保留用于审计；重复断开不改写时间。
 */
export function disconnectGmailConnection(
  connection: GmailConnectionRecord,
  now: Date,
): GmailConnectionRecord {
  if (connection.status === "disconnected" && connection.encryptedRefreshToken === null) {
    return connection;
  }
  return {
    ...connection,
    encryptedRefreshToken: null,
    status: "disconnected",
    lastErrorCode: null,
    updatedAt: requireTimestamp(now),
  };
}

/** 去除边界空白并拒绝缺失的可信字段。 */
function requireText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}

/** 校验服务端时钟并生成 UTC ISO 时间戳。 */
function requireTimestamp(now: Date): string {
  if (Number.isNaN(now.getTime())) {
    throw new RangeError("Gmail 连接时间无效");
  }
  return now.toISOString();
}
