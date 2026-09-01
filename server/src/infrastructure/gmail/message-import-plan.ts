import {
  buildImportCommitPlan,
  createImportPreview,
} from "../../application/index.js";
import type {
  ImportCommitPlan,
} from "../../application/index.js";
import { ApplicationError } from "../../application/index.js";
import {
  emailTradeToRawFields,
} from "../../domain/gmail/index.js";
import type {
  EmailTrade,
  GmailMessage,
} from "../../domain/gmail/index.js";
import type {
  ParsedTransaction,
} from "../../domain/ledger/index.js";
import {
  createParsedGmailMessageMetadata,
} from "./message-metadata.js";
import type {
  ParsedGmailMessageMetadata,
} from "./message-metadata.js";

/** 仓储按连接和 message ID 查出的既有最小状态。 */
export interface ExistingGmailMessageState {
  contentHash: string;
  accountId: string | null;
  parseStatus: "discovered" | "parsed" | "imported" | "rejected" | "conflict";
}

/** gmail_message_transactions 表所需的单笔关联，不复制交易业务字段。 */
export interface GmailMessageTransactionLink {
  connectionId: string;
  messageId: string;
  accountId: string;
  transactionHash: string;
  /** 邮件内从 1 开始的稳定成交顺序。 */
  ordinal: number;
}

/** 原子写事务成功后应保存的 imported 消息状态。 */
export type ImportedGmailMessageMetadata = Omit<
  ParsedGmailMessageMetadata,
  "parseStatus"
> & {
  parseStatus: "imported";
};

/** 新消息或可重试消息已经完成交易、FIFO 与关联计划。 */
export interface ReadyGmailMessageImportPlan {
  kind: "ready";
  metadata: ImportedGmailMessageMetadata;
  previewFingerprint: string;
  commit: ImportCommitPlan;
  links: readonly GmailMessageTransactionLink[];
}

/** 已成功导入的相同消息再次出现时不产生任何写入。 */
export interface AlreadyImportedGmailMessagePlan {
  kind: "already_imported";
  connectionId: string;
  messageId: string;
  contentHash: string;
}

/** Gmail 单消息确定性写入计划；数据库适配器必须在一个事务中执行 ready 分支。 */
export type GmailMessageImportPlan =
  | ReadyGmailMessageImportPlan
  | AlreadyImportedGmailMessagePlan;

/**
 * 把已完成 eConfirm 解析和账户映射的消息转换为账本原子写入计划。
 *
 * 邮件成交先桥接为 Schwab 标准八列，再复用文件导入的标准化、去重和 FIFO
 * 重放。相同 message ID 与内容摘要的 imported 记录直接幂等返回；相同 ID 的
 * 内容或账户发生变化则明确冲突，禁止覆盖既有金融事实。
 *
 * @param input - Gmail 元数据、已映射账户、当前账户交易快照和解析器版本。
 * @returns 已导入幂等结果，或需要由 DuckDB 单事务执行的完整变更计划。
 */
export function buildGmailMessageImportPlan(input: {
  connectionId: string;
  threadId: string | null;
  message: GmailMessage;
  trades: readonly EmailTrade[];
  accountId: string;
  /** 迁移期间沿用的 Schwab 完整账户键，用于生成跨来源一致的交易哈希。 */
  ledgerAccount: string;
  historyId: string | null;
  syncedAt: Date;
  parserVersion: string;
  baseLedgerVersion: number;
  existingMessage: ExistingGmailMessageState | null;
  existingAccountTransactions: readonly ParsedTransaction[];
}): GmailMessageImportPlan {
  const metadata = createParsedGmailMessageMetadata({
    connectionId: input.connectionId,
    threadId: input.threadId,
    message: input.message,
    trades: input.trades,
    accountId: input.accountId,
    historyId: input.historyId,
    syncedAt: input.syncedAt,
  });

  if (input.existingMessage !== null) {
    const contentChanged = input.existingMessage.contentHash !== metadata.contentHash;
    const accountChanged = input.existingMessage.accountId !== metadata.accountId;
    if (contentChanged || accountChanged) {
      throw new ApplicationError({
        code: "GMAIL_MESSAGE_CONFLICT",
        message: "同一 Gmail 消息的内容或账户归属已发生变化",
        statusCode: 409,
        retryable: false,
        details: {
          message_id: metadata.messageId,
          changed_fields: [
            ...(contentChanged ? ["content_hash"] : []),
            ...(accountChanged ? ["account_id"] : []),
          ],
        },
      });
    }
    if (input.existingMessage.parseStatus === "imported") {
      return {
        kind: "already_imported",
        connectionId: metadata.connectionId,
        messageId: metadata.messageId,
        contentHash: metadata.contentHash,
      };
    }
  }

  const existingTransactionHashes = new Set(
    input.existingAccountTransactions.map((transaction) => transaction.transactionHash),
  );
  const preview = createImportPreview({
    batch: {
      fileName: `gmail:${metadata.messageId}`,
      account: input.ledgerAccount,
      sourceHash: metadata.contentHash,
      sourceType: "gmail",
      sourceReference: metadata.messageId,
      rows: input.trades.map(emailTradeToRawFields),
    },
    parserVersion: input.parserVersion,
    baseLedgerVersion: input.baseLedgerVersion,
    existingTransactionHashes,
  });
  const commit = buildImportCommitPlan(preview, input.existingAccountTransactions);
  const importedMetadata: ImportedGmailMessageMetadata = {
    ...metadata,
    parseStatus: "imported",
  };

  return {
    kind: "ready",
    metadata: importedMetadata,
    previewFingerprint: preview.previewFingerprint,
    commit,
    links: preview.transactions.map((transaction, index) => ({
      connectionId: metadata.connectionId,
      messageId: metadata.messageId,
      accountId: input.accountId,
      transactionHash: transaction.transactionHash,
      ordinal: index + 1,
    })),
  };
}
