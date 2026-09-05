import type { DuckDBConnection } from "@duckdb/node-api";
import type { ParsedTransaction } from "../../domain/ledger/index.js";
import type { Lot, RealizedRecord } from "../../domain/fifo/index.js";
import type {
  ImportedGmailMessageMetadata,
  ReadyGmailMessageImportPlan,
} from "../gmail/message-import-plan.js";
import type { UnsuccessfulGmailMessageMetadata } from "../gmail/message-metadata.js";
import { ApplicationError } from "../../application/app-error.js";
import type { LyraDatabase } from "./database.js";

/** Gmail 单消息原子写入的稳定结果。 */
export interface PersistedGmailMessageImportResult {
  changed: boolean;
  ledgerVersion: number;
  insertedRows: number;
  skippedRows: number;
}

/**
 * 在单事务中执行一条 Gmail 消息的账本原子写入。
 *
 * 复用文件导入同一套标准交易/FIFO 写入形状，但来源是 gmail_import 版本且
 * 不经过 import_files 作业；消息元数据与交易关联在同一事务落库，任何失败
 * 整体回滚，不会留下"已导入消息却没有账本事实"的中间态。
 */
export async function persistGmailMessageImport(
  database: LyraDatabase,
  input: {
    plan: ReadyGmailMessageImportPlan;
    parserVersion: string;
    expectedLedgerVersion: number;
    syncedAt: Date;
  },
): Promise<PersistedGmailMessageImportResult> {
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    const current = await readCurrentLedgerVersion(connection);
    if (current !== input.expectedLedgerVersion) {
      throw new ApplicationError({
        code: "LEDGER_VERSION_CONFLICT",
        message: "账本版本已变化，必须重新同步 Gmail 消息",
        statusCode: 409,
        retryable: false,
        details: {
          expected_ledger_version: input.expectedLedgerVersion,
          actual_ledger_version: current,
        },
      });
    }
    let ledgerVersion = current;
    let insertedRows = 0;
    if (input.plan.commit.changed) {
      if (input.plan.commit.fifo === null) {
        throw new Error("变更 Gmail 消息计划缺少 FIFO 产物");
      }
      ledgerVersion = await createGmailLedgerVersion(
        connection,
        input.plan.metadata.messageId,
      );
      const accountId = input.plan.metadata.accountId;
      if (accountId === null) {
        throw new Error("已导入 Gmail 消息缺少账户归属");
      }
      for (const transaction of input.plan.commit.insertedTransactions) {
        await insertGmailTransaction(
          connection,
          transaction,
          accountId,
          input.parserVersion,
          ledgerVersion,
        );
        insertedRows += 1;
      }
      await connection.run(
        "DELETE FROM realized WHERE account_id = CAST($account_id AS UUID)",
        { account_id: accountId },
      );
      await connection.run(
        "DELETE FROM lots WHERE account_id = CAST($account_id AS UUID)",
        { account_id: accountId },
      );
      for (const lot of input.plan.commit.fifo.openLots) {
        await insertLot(connection, lot, accountId, ledgerVersion);
      }
      for (const realized of input.plan.commit.fifo.realizedRecords) {
        await insertRealized(connection, realized, accountId, ledgerVersion);
      }
      await connection.run(`
        INSERT INTO ledger_version_accounts (ledger_version, account_id)
        VALUES ($ledger_version, CAST($account_id AS UUID))
      `, { ledger_version: ledgerVersion, account_id: accountId });
    }
    await insertGmailMessageRow(connection, input.plan.metadata, input.syncedAt);
    for (const link of input.plan.links) {
      await connection.run(`
        INSERT INTO gmail_message_transactions (
          connection_id, message_id, account_id, transaction_hash, ordinal
        ) VALUES (
          CAST($connection_id AS UUID), $message_id, CAST($account_id AS UUID),
          $transaction_hash, $ordinal
        )
      `, {
        connection_id: link.connectionId,
        message_id: link.messageId,
        account_id: link.accountId,
        transaction_hash: link.transactionHash,
        ordinal: link.ordinal,
      });
    }
    await connection.run("COMMIT");
    return {
      changed: input.plan.commit.changed,
      ledgerVersion,
      insertedRows,
      skippedRows: input.plan.commit.skippedRows,
    };
  } catch (error: unknown) {
    try {
      await connection.run("ROLLBACK");
    } catch (rollbackError: unknown) {
      throw new AggregateError([error, rollbackError], "Gmail 消息提交失败且事务回滚失败");
    }
    throw error;
  }
}

/** 读取既有消息状态用于幂等与冲突判定；未见过返回 null。 */
export async function loadExistingGmailMessageState(
  database: LyraDatabase,
  connectionId: string,
  messageId: string,
): Promise<{
  contentHash: string;
  accountId: string | null;
  parseStatus: "discovered" | "parsed" | "imported" | "rejected" | "conflict";
} | null> {
  const reader = await database.connection.runAndReadAll(`
    SELECT content_hash, CAST(account_id AS VARCHAR) AS account_id, parse_status
    FROM gmail_messages
    WHERE connection_id = CAST($connection_id AS UUID)
      AND message_id = $message_id
  `, { connection_id: connectionId, message_id: messageId });
  const row = reader.getRowObjectsJson()[0];
  if (row === undefined || row.content_hash === null) {
    return null;
  }
  return {
    contentHash: requireText(row.content_hash, "Gmail 消息内容哈希"),
    accountId: row.account_id === null ? null : requireText(row.account_id, "Gmail 消息账户"),
    parseStatus: row.parse_status as "discovered" | "parsed" | "imported" | "rejected" | "conflict",
  };
}

/** 持久化解析拒绝或账户冲突的无正文消息记录。 */
export async function persistUnsuccessfulGmailMessage(
  database: LyraDatabase,
  metadata: UnsuccessfulGmailMessageMetadata,
  syncedAt: Date,
): Promise<void> {
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    await insertGmailMessageRow(connection, metadata, syncedAt);
    await connection.run("COMMIT");
  } catch (error: unknown) {
    try {
      await connection.run("ROLLBACK");
    } catch (rollbackError: unknown) {
      throw new AggregateError([error, rollbackError], "Gmail 消息归档失败且事务回滚失败");
    }
    throw error;
  }
}

async function insertGmailMessageRow(
  connection: DuckDBConnection,
  metadata: ImportedGmailMessageMetadata | UnsuccessfulGmailMessageMetadata,
  syncedAt: Date,
): Promise<void> {
  await connection.run(`
    INSERT INTO gmail_messages (
      connection_id, message_id, thread_id, received_at, content_hash,
      account_id, parse_status, error_code, transaction_count, history_id,
      synced_at
    ) VALUES (
      CAST($connection_id AS UUID), $message_id, $thread_id, $received_at,
      $content_hash, CAST($account_id AS UUID), $parse_status, $error_code,
      $transaction_count, $history_id, CAST($synced_at AS TIMESTAMPTZ)
    )
  `, {
    connection_id: metadata.connectionId,
    message_id: metadata.messageId,
    thread_id: metadata.threadId,
    received_at: metadata.receivedAt,
    content_hash: metadata.contentHash,
    account_id: metadata.accountId,
    parse_status: metadata.parseStatus,
    error_code: metadata.errorCode,
    transaction_count: metadata.transactionCount,
    history_id: metadata.historyId,
    synced_at: syncedAt.toISOString(),
  });
}

/** 创建 gmail_import 账本版本并登记受影响账户。 */
async function createGmailLedgerVersion(
  connection: DuckDBConnection,
  messageId: string,
): Promise<number> {
  const reader = await connection.runAndReadAll(`
    INSERT INTO ledger_versions (reason, source_reference)
    VALUES ('gmail_import', $source_reference)
    RETURNING version
  `, { source_reference: messageId });
  const version = reader.getRowObjectsJson()[0]?.version;
  const parsed = typeof version === "number"
    ? version
    : Number.parseInt(String(version ?? ""), 10);
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new Error("Gmail 账本版本无效");
  }
  return parsed;
}

/** 插入一条来源为 Gmail 的标准交易事实；import_file_id 保持 NULL。 */
async function insertGmailTransaction(
  connection: DuckDBConnection,
  transaction: ParsedTransaction,
  accountId: string,
  parserVersion: string,
  ledgerVersion: number,
): Promise<void> {
  await connection.run(`
    INSERT INTO transactions (
      account_id, record_date, transaction_date, action, raw_symbol, description,
      asset_type, underlying, expiry, strike, option_type, quantity, price, fees,
      amount, source_type, source_reference, import_file_id, source_hash, sequence,
      occurrence, business_hash, transaction_hash, parser_version, ledger_version
    ) VALUES (
      CAST($account_id AS UUID), CAST($record_date AS DATE), CAST($transaction_date AS DATE),
      $action, $raw_symbol, $description, $asset_type, $underlying,
      CAST($expiry AS DATE), CAST($strike AS DECIMAL(18, 4)), $option_type,
      CAST($quantity AS DECIMAL(18, 6)), CAST($price AS DECIMAL(18, 6)),
      CAST($fees AS DECIMAL(18, 6)), CAST($amount AS DECIMAL(18, 4)),
      $source_type, $source_reference, NULL, $source_hash,
      $sequence, $occurrence, $business_hash, $transaction_hash, $parser_version,
      $ledger_version
    )
  `, {
    account_id: accountId,
    record_date: transaction.recordDate,
    transaction_date: transaction.transactionDate,
    action: transaction.action,
    raw_symbol: transaction.rawSymbol,
    description: transaction.description,
    asset_type: transaction.assetType,
    underlying: transaction.underlying,
    expiry: transaction.expiry,
    strike: transaction.strike?.toString() ?? null,
    option_type: transaction.optionType,
    quantity: transaction.quantity?.toString() ?? null,
    price: transaction.price?.toString() ?? null,
    fees: transaction.fees?.toString() ?? null,
    amount: transaction.amount?.toString() ?? null,
    source_type: transaction.sourceType,
    source_reference: transaction.sourceReference,
    source_hash: transaction.sourceHash,
    sequence: transaction.sequence,
    occurrence: transaction.occurrence,
    business_hash: transaction.businessHash,
    transaction_hash: transaction.transactionHash,
    parser_version: parserVersion,
    ledger_version: ledgerVersion,
  });
}

async function insertLot(
  connection: DuckDBConnection,
  lot: Lot,
  accountId: string,
  ledgerVersion: number,
): Promise<void> {
  if (lot.symbolKey === null || lot.underlying === null) {
    throw new Error("FIFO 未平批次缺少合约或标的");
  }
  await connection.run(`
    INSERT INTO lots (
      account_id, symbol_key, asset_type, underlying, expiry, strike, option_type,
      direction, open_date, open_price, open_quantity, remaining_quantity,
      open_fees, fees_allocated, open_transaction_hash, ledger_version
    ) VALUES (
      CAST($account_id AS UUID), $symbol_key, $asset_type, $underlying,
      CAST($expiry AS DATE), CAST($strike AS DECIMAL(18, 4)), $option_type,
      $direction, CAST($open_date AS DATE), CAST($open_price AS DECIMAL(18, 6)),
      CAST($open_quantity AS DECIMAL(18, 6)), CAST($remaining_quantity AS DECIMAL(18, 6)),
      CAST($open_fees AS DECIMAL(18, 6)), CAST($fees_allocated AS DECIMAL(18, 6)),
      $open_transaction_hash, $ledger_version
    )
  `, {
    account_id: accountId,
    symbol_key: lot.symbolKey,
    asset_type: lot.assetType,
    underlying: lot.underlying,
    expiry: lot.expiry,
    strike: lot.strike?.toString() ?? null,
    option_type: lot.optionType,
    direction: lot.direction,
    open_date: lot.openDate,
    open_price: lot.openPrice.toString(),
    open_quantity: lot.openQuantity.toString(),
    remaining_quantity: lot.remainingQuantity.toString(),
    open_fees: lot.openFees.toString(),
    fees_allocated: lot.feesAllocated.toString(),
    open_transaction_hash: lot.openTransaction,
    ledger_version: ledgerVersion,
  });
}

async function insertRealized(
  connection: DuckDBConnection,
  realized: RealizedRecord,
  accountId: string,
  ledgerVersion: number,
): Promise<void> {
  if (realized.symbolKey === null || realized.underlying === null) {
    throw new Error("FIFO 已实现记录缺少合约或标的");
  }
  await connection.run(`
    INSERT INTO realized (
      account_id, symbol_key, asset_type, underlying, direction, open_date,
      close_date, quantity, open_price, close_price, open_fees, close_fees, pnl,
      close_action, open_transaction_hash, close_transaction_hash, ledger_version
    ) VALUES (
      CAST($account_id AS UUID), $symbol_key, $asset_type, $underlying, $direction,
      CAST($open_date AS DATE), CAST($close_date AS DATE),
      CAST($quantity AS DECIMAL(18, 6)), CAST($open_price AS DECIMAL(18, 6)),
      CAST($open_fees AS DECIMAL(18, 6)), CAST($close_fees AS DECIMAL(18, 6)),
      CAST($pnl AS DECIMAL(18, 4)),
      $close_action, $open_transaction_hash, $close_transaction_hash, $ledger_version
    )
  `, {
    account_id: accountId,
    symbol_key: realized.symbolKey,
    asset_type: realized.assetType,
    underlying: realized.underlying,
    direction: realized.direction,
    open_date: realized.openDate,
    close_date: realized.closeDate,
    quantity: realized.quantity.toString(),
    open_price: realized.openPrice.toString(),
    open_fees: realized.openFees.toString(),
    close_fees: realized.closeFees.toString(),
    pnl: realized.pnl.toString(),
    close_action: realized.closeAction,
    open_transaction_hash: realized.openTransaction,
    close_transaction_hash: realized.closeTransaction,
    ledger_version: ledgerVersion,
  });
}

async function readCurrentLedgerVersion(connection: DuckDBConnection): Promise<number> {
  const reader = await connection.runAndReadAll(
    "SELECT coalesce(max(version), 0) AS version FROM ledger_versions",
  );
  const version = reader.getRowObjectsJson()[0]?.version;
  const parsed = typeof version === "number"
    ? version
    : Number.parseInt(String(version ?? "0"), 10);
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new Error("当前账本版本无效");
  }
  return parsed;
}

function requireText(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${label}与数据库 schema 不一致`);
  }
  return value;
}
