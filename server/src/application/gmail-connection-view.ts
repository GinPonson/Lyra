import type {
  GmailConnectionRecord,
} from "../infrastructure/gmail/connection-credential.js";
import { isGmailSyncErrorCode } from "../domain/gmail/index.js";

/** 账户中心可展示的 Gmail 连接状态，不包含任何可继续访问邮箱的秘密。 */
export interface PublicGmailConnection {
  id: string;
  googleEmail: string;
  status: GmailConnectionRecord["status"];
  lastErrorCode: string | null;
  lastSyncAt: string | null;
  createdAt: string;
  updatedAt: string;
}

/**
 * 把 Gmail 持久化记录投影为 App 可见连接状态。
 *
 * Google subject、授权 scope、history 游标与 refresh token 密文只供服务端认证
 * 和增量同步使用；即使密文不可读，也不得把它们放进客户端响应或本地缓存。
 */
export function createPublicGmailConnection(
  connection: GmailConnectionRecord,
): PublicGmailConnection {
  if (
    connection.lastErrorCode !== null
    && !isGmailSyncErrorCode(connection.lastErrorCode)
  ) {
    throw new RangeError("Gmail 连接包含非稳定错误码");
  }
  return {
    id: connection.id,
    googleEmail: connection.googleEmail,
    status: connection.status,
    lastErrorCode: connection.lastErrorCode,
    lastSyncAt: connection.lastSyncAt,
    createdAt: connection.createdAt,
    updatedAt: connection.updatedAt,
  };
}
