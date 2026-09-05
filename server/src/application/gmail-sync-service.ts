import type { SecretCipher } from "../infrastructure/security/index.js";
import type { GmailConnectionRecord } from "../infrastructure/gmail/connection-credential.js";
import type { GoogleOAuthFetch } from "../infrastructure/gmail/google-oauth-client.js";
import { refreshGmailConnectionAccessToken } from "../infrastructure/gmail/google-oauth-client.js";
import {
  fetchIncrementalGmailFullRecords,
  fetchInitialGmailFullRecords,
} from "../infrastructure/gmail/google-gmail-client.js";
import {
  parseGmailFullInput,
} from "../infrastructure/gmail/full-message.js";
import type { EmailTrade, GmailMessage } from "../domain/gmail/index.js";
import {
  extractAccountSuffix,
  parseEconfirm,
  GmailSyncError,
  markGmailHistoryExpired,
} from "../domain/gmail/index.js";
import {
  completeGmailSync,
  decideGmailIncrementalFetch,
  planGmailSync,
} from "../domain/gmail/index.js";
import { buildSchwabEconfirmSearch } from "../infrastructure/gmail/search-query.js";
import {
  buildGmailMessageImportPlan,
} from "../infrastructure/gmail/message-import-plan.js";
import type { GmailMessageImportPlan } from "../infrastructure/gmail/message-import-plan.js";
import {
  loadExistingGmailMessageState,
  persistGmailMessageImport,
  persistUnsuccessfulGmailMessage,
} from "../infrastructure/db/gmail-message-repository.js";
import {
  createGmailAccountMappingRow,
  loadGmailAccountMappingRows,
  loadGmailConnectionRow,
  saveGmailConnectionRow,
} from "../infrastructure/db/gmail-connection-repository.js";
import { loadImportAccounts } from "../infrastructure/db/import-job-repository.js";
import { loadLedgerAnalysisSnapshot } from "../infrastructure/db/ledger-analysis-repository.js";
import { readCurrentLedgerVersion } from "../infrastructure/db/agent-session-repository.js";
import { IMPORT_PARSER_VERSION } from "./import-service.js";
import { resolveGmailAccountMapping } from "./gmail-account-mapping.js";
import { createUnsuccessfulGmailMessageMetadata } from "../infrastructure/gmail/message-metadata.js";
import type { GmailGoogleOAuthConfig } from "./gmail-service.js";
import { ApplicationError } from "./app-error.js";
import type { WriteCoordinator } from "./write-coordinator.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";

/** 单次同步任务的消息硬上限；截断必须由用户确认后重扫。 */
const DEFAULT_SYNC_MAXIMUM_MESSAGES = 25;

/** 一次同步的稳定汇总，不携带邮件正文或供应商原始响应。 */
export interface GmailSyncHttpResponse {
  mode: "incremental" | "bounded_rescan";
  truncated: boolean;
  importedMessages: number;
  alreadyImportedMessages: number;
  conflictMessages: number;
  rejectedMessages: number;
  ignoredMessages: number;
  connection: {
    id: string;
    status: string;
    historyId: string | null;
    lastSyncAt: string | null;
  };
}

/**
 * Gmail 同步编排器：把状态机决策、token 刷新、有界取数、确定性解析、账户
 * 映射和账本原子写入装配成一次完整同步。
 *
 * 游标只在全部消息都有持久终态后推进；截断进入 needs_resync 并保留旧游标；
 * 临时失败整体抛出且不改动连接状态，符合"不使用隐式回退"的产品原则。
 */
export class GmailSyncService {
  readonly #database: LyraDatabase;
  readonly #writeCoordinator: WriteCoordinator;
  readonly #cipher: SecretCipher;
  readonly #googleOAuth: GmailGoogleOAuthConfig | null;
  readonly #fetchFn: GoogleOAuthFetch | undefined;
  readonly #syncMaximumMessages: number;
  readonly #now: () => Date;

  constructor(input: {
    database: LyraDatabase;
    writeCoordinator: WriteCoordinator;
    cipher: SecretCipher;
    googleOAuth: GmailGoogleOAuthConfig | null;
    fetchFn?: GoogleOAuthFetch;
    syncMaximumMessages?: number;
    now?: () => Date;
  }) {
    this.#database = input.database;
    this.#writeCoordinator = input.writeCoordinator;
    this.#cipher = input.cipher;
    this.#googleOAuth = input.googleOAuth;
    this.#fetchFn = input.fetchFn;
    this.#syncMaximumMessages = input.syncMaximumMessages ?? DEFAULT_SYNC_MAXIMUM_MESSAGES;
    this.#now = input.now ?? (() => new Date());
  }

  async sync(input: {
    connectionId: string;
    confirmedRescanAfterDate?: string | null;
    explicitAccountId?: string | null;
  }): Promise<GmailSyncHttpResponse> {
    const connection = await loadGmailConnectionRow(this.#database, input.connectionId);
    if (connection === null) {
      throw new ApplicationError({
        code: "GMAIL_CONNECTION_ERROR",
        message: "Gmail 连接不存在",
        statusCode: 404,
        retryable: false,
      });
    }
    const state = {
      status: connection.status,
      historyId: connection.historyId,
      lastSyncAt: connection.lastSyncAt,
      lastErrorCode: connection.lastErrorCode,
    };
    const confirmedDate = input.confirmedRescanAfterDate ?? undefined;
    let request;
    try {
      request = planGmailSync(state, confirmedDate);
    } catch (error: unknown) {
      throw this.#mapSyncError(error);
    }
    if (this.#googleOAuth === null) {
      throw new ApplicationError({
        code: "GMAIL_CONNECTION_ERROR",
        message: "Google OAuth 客户端尚未配置",
        statusCode: 503,
        retryable: false,
      });
    }

    let accessToken: string;
    try {
      const refreshed = await refreshGmailConnectionAccessToken(
        connection,
        this.#cipher,
        this.#googleOAuth,
        this.#now(),
        this.#fetchFn,
      );
      accessToken = refreshed.accessToken;
    } catch (error: unknown) {
      // refreshGoogleAccessToken 把端点失败包装为 GmailSyncError：
      // 400/401/403 属于授权失效（连接进入 error 状态），其余保持可重试。
      if (error instanceof GmailSyncError
        && error.code === "GMAIL_AUTH_REVOKED") {
        await saveGmailConnectionRow(this.#database, {
          ...connection,
          status: "error",
          lastErrorCode: "GMAIL_AUTH_REVOKED",
          updatedAt: this.#now().toISOString(),
        }, connection.updatedAt);
        throw new ApplicationError({
          code: "GMAIL_AUTH_REVOKED",
          message: "Gmail 授权已失效，需要重新授权",
          statusCode: 401,
          retryable: false,
        });
      }
      if (error instanceof GmailSyncError
        && error.code === "GMAIL_CONNECTION_ERROR") {
        throw new ApplicationError({
          code: "GMAIL_CONNECTION_ERROR",
          message: "Google 服务暂时不可用，请稍后重试",
          statusCode: 503,
          retryable: true,
        });
      }
      throw new ApplicationError({
        code: "GMAIL_CONNECTION_ERROR",
        message: "Google 服务暂时不可用，请稍后重试",
        statusCode: 503,
        retryable: true,
      });
    }

    if (request.mode === "incremental") {
      let fetched;
      try {
        fetched = await fetchIncrementalGmailFullRecords(
          accessToken,
          {
            startHistoryId: request.startHistoryId,
            maximumMessages: this.#syncMaximumMessages,
          },
          this.#fetchFn,
        );
      } catch (error: unknown) {
        if (error instanceof GmailSyncError
          && error.code === "GMAIL_HISTORY_EXPIRED") {
          const expired = markGmailHistoryExpired(state, "GMAIL_HISTORY_EXPIRED");
          await saveGmailConnectionRow(this.#database, {
            ...connection,
            status: expired.status,
            lastErrorCode: expired.lastErrorCode,
            updatedAt: this.#now().toISOString(),
          }, connection.updatedAt);
          throw new ApplicationError({
            code: "GMAIL_HISTORY_EXPIRED",
            message: "Gmail 增量游标已失效，必须确认重扫范围",
            statusCode: 409,
            retryable: false,
          });
        }
        throw error;
      }
      const decision = decideGmailIncrementalFetch(state, {
        observedHistoryId: fetched.observedHistoryId,
        candidateMessageCount: fetched.records.length,
        truncated: fetched.truncated,
      });
      if (decision.kind === "needs_resync") {
        await saveGmailConnectionRow(this.#database, {
          ...connection,
          status: decision.state.status,
          lastErrorCode: decision.state.lastErrorCode,
          updatedAt: this.#now().toISOString(),
        }, connection.updatedAt);
        return {
          mode: "incremental",
          truncated: true,
          importedMessages: 0,
          alreadyImportedMessages: 0,
          conflictMessages: 0,
          rejectedMessages: 0,
          ignoredMessages: fetched.ignoredMessageCount,
          connection: {
            id: connection.id,
            status: decision.state.status,
            historyId: connection.historyId,
            lastSyncAt: connection.lastSyncAt,
          },
        };
      }
      const summary = await this.#processMessages({
        connection,
        records: fetched.records,
        explicitAccountId: input.explicitAccountId ?? null,
      });
      await this.#complete(connection, decision.completionHistoryId);
      return {
        mode: "incremental",
        truncated: false,
        ...summary,
        ignoredMessages: fetched.ignoredMessageCount,
        connection: {
          id: connection.id,
          status: "active",
          historyId: decision.completionHistoryId,
          lastSyncAt: this.#now().toISOString(),
        },
      };
    }

    const plan = buildSchwabEconfirmSearch(
      request.afterDate,
      this.#syncMaximumMessages,
    );
    const fetched = await fetchInitialGmailFullRecords(
      accessToken,
      connection.googleEmail,
      plan,
      this.#fetchFn,
    );
    if (fetched.kind === "truncated") {
      await saveGmailConnectionRow(this.#database, {
        ...connection,
        status: "needs_resync",
        lastErrorCode: "GMAIL_RESYNC_CONFIRMATION_REQUIRED",
        updatedAt: this.#now().toISOString(),
      }, connection.updatedAt);
      return {
        mode: "bounded_rescan",
        truncated: true,
        importedMessages: 0,
        alreadyImportedMessages: 0,
        conflictMessages: 0,
        rejectedMessages: 0,
        ignoredMessages: 0,
        connection: {
          id: connection.id,
          status: "needs_resync",
          historyId: connection.historyId,
          lastSyncAt: connection.lastSyncAt,
        },
      };
    }
    const summary = await this.#processMessages({
      connection,
      records: fetched.records,
      explicitAccountId: input.explicitAccountId ?? null,
    });
    await this.#complete(connection, fetched.completionHistoryId);
    return {
      mode: "bounded_rescan",
      truncated: false,
      ...summary,
      ignoredMessages: 0,
      connection: {
        id: connection.id,
        status: "active",
        historyId: fetched.completionHistoryId,
        lastSyncAt: this.#now().toISOString(),
      },
    };
  }

  /** 逐封消息完成解析、映射与账本原子写入；所有终态都持久化。 */
  async #processMessages(input: {
    connection: { id: string; historyId: string | null };
    records: readonly Readonly<Record<string, unknown>>[];
    explicitAccountId: string | null;
  }): Promise<{
    importedMessages: number;
    alreadyImportedMessages: number;
    conflictMessages: number;
    rejectedMessages: number;
  }> {
    const summary = {
      importedMessages: 0,
      alreadyImportedMessages: 0,
      conflictMessages: 0,
      rejectedMessages: 0,
    };
    const accounts = await loadImportAccounts(this.#database);
    for (const record of input.records) {
      const messages = parseGmailFullInput(record, "Gmail 同步");
      for (const message of messages) {
        let trades: readonly EmailTrade[];
        try {
          trades = parseEconfirm(message);
        } catch {
          await this.#persistUnsuccessful(input.connection.id, message, input.connection.historyId, "GMAIL_MESSAGE_REJECTED");
          summary.rejectedMessages += 1;
          continue;
        }
        const suffix = extractAccountSuffix(message);
        const existingMappingRows = await loadGmailAccountMappingRows(
          this.#database,
          input.connection.id,
        );
        const existingMapping = existingMappingRows.find((row) => (
          row.accountSuffix.toUpperCase() === suffix
        )) ?? null;
        let mapping;
        try {
          mapping = resolveGmailAccountMapping({
            connectionId: input.connection.id,
            accountSuffix: suffix,
            accounts,
            existingMapping,
            explicitAccountId: input.explicitAccountId,
          });
        } catch (error: unknown) {
          if (error instanceof ApplicationError
            && error.code === "ACCOUNT_MAPPING_REQUIRED") {
            await this.#persistUnsuccessful(
              input.connection.id,
              message,
              input.connection.historyId,
              "ACCOUNT_MAPPING_REQUIRED",
            );
            summary.conflictMessages += 1;
            continue;
          }
          throw error;
        }
        if (mapping.mappingToCreate !== null) {
          await createGmailAccountMappingRow(this.#database, {
            connectionId: mapping.mappingToCreate.connectionId,
            accountSuffix: mapping.mappingToCreate.accountSuffix,
            accountId: mapping.mappingToCreate.accountId,
            at: this.#now().toISOString(),
          });
        }
        const outcome = await this.#writeCoordinator.enqueue(async () => {
          const ledgerVersion = await readCurrentLedgerVersion(this.#database);
          const snapshot = await loadLedgerAnalysisSnapshot(
            this.#database,
            ledgerVersion,
          );
          const existingMessage = await loadExistingGmailMessageState(
            this.#database,
            input.connection.id,
            message.messageId,
          );
          const plan: GmailMessageImportPlan = buildGmailMessageImportPlan({
            connectionId: input.connection.id,
            threadId: null,
            message,
            trades,
            accountId: mapping.accountId,
            ledgerAccount: mapping.legacyAccountKey,
            historyId: input.connection.historyId,
            syncedAt: this.#now(),
            parserVersion: IMPORT_PARSER_VERSION,
            baseLedgerVersion: ledgerVersion,
            existingMessage,
            existingAccountTransactions: snapshot.transactions.filter(
              (transaction) => transaction.account === mapping.legacyAccountKey,
            ),
          });
          if (plan.kind === "already_imported") {
            return plan;
          }
          await persistGmailMessageImport(this.#database, {
            plan,
            parserVersion: IMPORT_PARSER_VERSION,
            expectedLedgerVersion: ledgerVersion,
            syncedAt: this.#now(),
          });
          return plan;
        });
        if (outcome.kind === "already_imported") {
          summary.alreadyImportedMessages += 1;
        } else {
          summary.importedMessages += 1;
        }
      }
    }
    return summary;
  }

  async #persistUnsuccessful(
    connectionId: string,
    message: GmailMessage,
    historyId: string | null,
    errorCode: "GMAIL_MESSAGE_REJECTED" | "ACCOUNT_MAPPING_REQUIRED",
  ): Promise<void> {
    const metadata = createUnsuccessfulGmailMessageMetadata({
      connectionId,
      threadId: null,
      message,
      accountId: null,
      historyId,
      syncedAt: this.#now(),
      errorCode,
    });
    await this.#writeCoordinator.enqueue(() => persistUnsuccessfulGmailMessage(
      this.#database,
      metadata,
      this.#now(),
    ));
  }

  /** 推进游标到 active 状态；所有消息已有持久终态后才允许调用。 */
  async #complete(
    connection: GmailConnectionRecord,
    completionHistoryId: string,
  ): Promise<void> {
    const completed = completeGmailSync(
      {
        status: "active",
        historyId: null,
        lastSyncAt: null,
        lastErrorCode: null,
      },
      completionHistoryId,
      this.#now().toISOString(),
    );
    await saveGmailConnectionRow(this.#database, {
      ...connection,
      status: completed.status,
      historyId: completed.historyId,
      lastSyncAt: completed.lastSyncAt,
      lastErrorCode: completed.lastErrorCode,
      updatedAt: this.#now().toISOString(),
    }, connection.updatedAt);
  }

  #mapSyncError(error: unknown): ApplicationError {
    if (error instanceof Error && error.name === "GmailSyncError") {
      const code = (error as { code?: unknown }).code;
      if (code === "GMAIL_RESYNC_CONFIRMATION_REQUIRED") {
        return new ApplicationError({
          code,
          message: "Gmail 首次扫描或重扫必须确认起始日期",
          statusCode: 409,
          retryable: false,
        });
      }
      if (code === "GMAIL_AUTH_REVOKED") {
        return new ApplicationError({
          code,
          message: "Gmail 连接已断开，需要重新授权",
          statusCode: 401,
          retryable: false,
        });
      }
      if (code === "GMAIL_HISTORY_EXPIRED") {
        return new ApplicationError({
          code,
          message: "Gmail 增量游标不可用，必须确认重扫范围",
          statusCode: 409,
          retryable: false,
        });
      }
      return new ApplicationError({
        code: "GMAIL_CONNECTION_ERROR",
        message: "Gmail 连接处于错误状态",
        statusCode: 409,
        retryable: false,
      });
    }
    if (error instanceof ApplicationError) {
      return error;
    }
    return new ApplicationError({
      code: "GMAIL_CONNECTION_ERROR",
      message: "Gmail 同步失败",
      statusCode: 503,
      retryable: true,
    });
  }
}
