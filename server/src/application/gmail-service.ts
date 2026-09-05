import { randomUUID } from "node:crypto";

import type { SecretCipher } from "../infrastructure/security/index.js";
import type { GoogleOAuthFetch } from "../infrastructure/gmail/google-oauth-client.js";
import {
  buildGoogleAuthorizationRequest,
  exchangeGoogleAuthorizationCode,
  revokeGmailConnectionAuthorization,
} from "../infrastructure/gmail/google-oauth-client.js";
import {
  consumeGoogleOAuthTransaction,
  createGoogleOAuthTransaction,
  type GoogleOAuthTransactionRecord,
} from "../infrastructure/gmail/oauth-transaction.js";
import {
  createGmailConnectionRecord,
  disconnectGmailConnection,
} from "../infrastructure/gmail/connection-credential.js";
import type { GmailConnectionRecord } from "../infrastructure/gmail/connection-credential.js";
import {
  createGmailConnectionRow,
  loadGmailAccountMappingRow,
  loadGmailAccountMappingRows,
  loadGmailConnectionRow,
  loadGmailConnectionRows,
  saveGmailConnectionRow,
  updateGmailAccountMappingRow,
} from "../infrastructure/db/gmail-connection-repository.js";
import { loadImportAccounts } from "../infrastructure/db/import-job-repository.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";
import { ApplicationError } from "./app-error.js";
import {
  planGmailAccountMappingUpdate,
} from "./gmail-account-mapping.js";
import {
  createPublicGmailAccountMappingList,
} from "./gmail-account-mapping-view.js";
import type { PublicGmailAccountMapping } from "./gmail-account-mapping-view.js";
import { createPublicGmailConnection } from "./gmail-connection-view.js";
import type { PublicGmailConnection } from "./gmail-connection-view.js";

/** Google OAuth 客户端配置；未配置时授权路由明确降级而不是静默失败。 */
export interface GmailGoogleOAuthConfig {
  clientId: string;
  clientSecret?: string;
  redirectUri: string;
}

/** Gmail 连接、映射与授权事务的应用层编排。 */
export class GmailService {
  readonly #database: LyraDatabase;
  readonly #cipher: SecretCipher;
  readonly #googleOAuth: GmailGoogleOAuthConfig | null;
  readonly #fetchFn: GoogleOAuthFetch | undefined;
  readonly #now: () => Date;
  readonly #transactions = new Map<string, GoogleOAuthTransactionRecord>();

  constructor(input: {
    database: LyraDatabase;
    cipher: SecretCipher;
    googleOAuth: GmailGoogleOAuthConfig | null;
    fetchFn?: GoogleOAuthFetch;
    now?: () => Date;
  }) {
    this.#database = input.database;
    this.#cipher = input.cipher;
    this.#googleOAuth = input.googleOAuth;
    this.#fetchFn = input.fetchFn;
    this.#now = input.now ?? (() => new Date());
  }

  /**
   * 创建绑定当前设备的短时 OAuth 授权事务。
   *
   * record 只保存在服务端进程内存（state 哈希与 PKCE verifier 永不出站）；
   * 未配置 Google OAuth 客户端时返回 null，由路由转换为稳定降级错误。
   */
  createAuthorizationTransaction(deviceId: string): {
    authorizationUrl: string;
    expiresAt: string;
  } | null {
    if (this.#googleOAuth === null) {
      return null;
    }
    const transaction = createGoogleOAuthTransaction(deviceId, this.#now(), 10 * 60 * 1000);
    this.#transactions.set(transaction.state, transaction.record);
    const request = buildGoogleAuthorizationRequest(
      transaction,
      this.#googleOAuth.clientId,
      this.#googleOAuth.redirectUri,
    );
    return { authorizationUrl: request.authorizationUrl, expiresAt: request.expiresAt };
  }

  /** 消费一次性事务、与 Google 交换离线授权并创建 pending_initial_scan 连接。 */
  async completeAuthorizationTransaction(input: {
    deviceId: string;
    state: string;
    authorizationCode: string;
  }): Promise<PublicGmailConnection> {
    if (this.#googleOAuth === null) {
      throw new ApplicationError({
        code: "GMAIL_CONNECTION_ERROR",
        message: "Google OAuth 客户端尚未配置",
        statusCode: 503,
        retryable: false,
      });
    }
    const stored = this.#transactions.get(input.state);
    if (stored === undefined) {
      throw new ApplicationError({
        code: "GOOGLE_AUTH_TRANSACTION_INVALID",
        message: "Google 授权事务无效或已过期",
        statusCode: 400,
        retryable: false,
      });
    }
    const consumed = this.#mapGmailOAuthError(() => consumeGoogleOAuthTransaction(
      stored,
      input.state,
      input.deviceId,
      this.#now(),
    ));
    this.#transactions.set(input.state, consumed);
    let exchanged;
    try {
      exchanged = await exchangeGoogleAuthorizationCode({
        authorizationCode: input.authorizationCode,
        codeVerifier: consumed.codeVerifier,
        clientId: this.#googleOAuth.clientId,
        ...(this.#googleOAuth.clientSecret === undefined
          ? {}
          : { clientSecret: this.#googleOAuth.clientSecret }),
        redirectUri: this.#googleOAuth.redirectUri,
        now: this.#now(),
      }, this.#fetchFn);
    } catch (error: unknown) {
      this.#transactions.delete(input.state);
      throw this.#mapGoogleTransportError(error);
    }
    const record = createGmailConnectionRecord({
      connectionId: randomUUID(),
      identity: exchanged.identity,
      grant: exchanged.grant,
      cipher: this.#cipher,
      now: this.#now(),
    });
    try {
      await createGmailConnectionRow(this.#database, record);
    } catch (error: unknown) {
      this.#transactions.delete(input.state);
      if (error instanceof Error && /unique/i.test(error.message)) {
        throw new ApplicationError({
          code: "GMAIL_CONNECTION_ERROR",
          message: "该 Google 账号已存在 Gmail 连接",
          statusCode: 409,
          retryable: false,
        });
      }
      throw error;
    }
    this.#transactions.delete(input.state);
    return createPublicGmailConnection(record);
  }

  /** 连接列表按创建时间与 ID 稳定排序，只含公开字段。 */
  async listConnections(): Promise<PublicGmailConnection[]> {
    const records = await loadGmailConnectionRows(this.#database);
    return records.map((record) => createPublicGmailConnection(record));
  }

  /** 读取一个连接的映射安全公开列表。 */
  async listMappings(connectionId: string): Promise<readonly PublicGmailAccountMapping[]> {
    await this.#requireConnection(connectionId);
    const rows = await loadGmailAccountMappingRows(this.#database, connectionId);
    return createPublicGmailAccountMappingList(connectionId, rows);
  }

  /** 按显式改绑规则更新映射目标账户；相同目标幂等。 */
  async updateMapping(input: {
    connectionId: string;
    mappingId: string;
    targetAccountId: string;
  }): Promise<PublicGmailAccountMapping> {
    await this.#requireConnection(input.connectionId);
    const mapping = await loadGmailAccountMappingRow(this.#database, input.mappingId);
    if (mapping === null) {
      throw new ApplicationError({
        code: "GMAIL_CONNECTION_ERROR",
        message: "Gmail 账户映射不存在",
        statusCode: 404,
        retryable: false,
      });
    }
    const plan = planGmailAccountMappingUpdate({
      connectionId: input.connectionId,
      mapping,
      accounts: await loadImportAccounts(this.#database),
      targetAccountId: input.targetAccountId,
    });
    if (plan.changed) {
      await updateGmailAccountMappingRow(this.#database, {
        mappingId: input.mappingId,
        expectedAccountId: plan.expectedAccountId,
        nextAccountId: plan.nextMapping.accountId,
        at: this.#now().toISOString(),
      });
    }
    const updated = await loadGmailAccountMappingRow(this.#database, input.mappingId);
    if (updated === null) {
      throw new ApplicationError({
        code: "GMAIL_CONNECTION_ERROR",
        message: "Gmail 账户映射不存在",
        statusCode: 404,
        retryable: false,
      });
    }
    const publicMappings = createPublicGmailAccountMappingList(
      input.connectionId,
      [updated],
    );
    const publicMapping = publicMappings[0];
    if (publicMapping === undefined) {
      throw new ApplicationError({
        code: "GMAIL_CONNECTION_ERROR",
        message: "Gmail 账户映射不存在",
        statusCode: 404,
        retryable: false,
      });
    }
    return publicMapping;
  }

  /**
   * 断开连接；可选先请求 Google 远端撤销。
   *
   * 远端撤销失败时不假定已撤销、不清除本地密文，调用方可重试；纯本地断开
   * 幂等清除 refresh token 密文并保留审计游标。
   */
  async disconnect(input: {
    connectionId: string;
    revoke: boolean;
  }): Promise<PublicGmailConnection> {
    const connection = await this.#requireConnection(input.connectionId);
    let next: GmailConnectionRecord = connection;
    if (input.revoke && connection.encryptedRefreshToken !== null) {
      try {
        next = await revokeGmailConnectionAuthorization(
          connection,
          this.#cipher,
          this.#now(),
          this.#fetchFn,
        );
      } catch (error: unknown) {
        throw this.#mapGoogleTransportError(error);
      }
    } else {
      next = disconnectGmailConnection(connection, this.#now());
    }
    if (next.updatedAt !== connection.updatedAt) {
      await saveGmailConnectionRow(this.#database, next, connection.updatedAt);
    }
    return createPublicGmailConnection(next);
  }

  async #requireConnection(connectionId: string): Promise<GmailConnectionRecord> {    const connection = await loadGmailConnectionRow(this.#database, connectionId);
    if (connection === null) {
      throw new ApplicationError({
        code: "GMAIL_CONNECTION_ERROR",
        message: "Gmail 连接不存在",
        statusCode: 404,
        retryable: false,
      });
    }
    return connection;
  }

  #mapGmailOAuthError<T>(run: () => T): T {
    try {
      return run();
    } catch (error: unknown) {
      const code = (error as { code?: unknown }).code;
      if (
        code === "GOOGLE_AUTH_TRANSACTION_INVALID"
        || code === "GOOGLE_AUTH_TRANSACTION_EXPIRED"
        || code === "GOOGLE_AUTH_TRANSACTION_USED"
      ) {
        throw new ApplicationError({
          code: "GOOGLE_AUTH_TRANSACTION_INVALID",
          message: "Google 授权事务无效、已过期或已使用",
          statusCode: 400,
          retryable: false,
        });
      }
      throw error;
    }
  }

  #mapGoogleTransportError(error: unknown): ApplicationError | unknown {
    if (error instanceof ApplicationError) {
      return error;
    }
    if (error instanceof Error && error.name.startsWith("Google")) {
      return new ApplicationError({
        code: "GMAIL_CONNECTION_ERROR",
        message: "Google 服务暂时不可用，请稍后重试",
        statusCode: 503,
        retryable: true,
      });
    }
    return error;
  }
}
