import type { DuckDBConnection } from "@duckdb/node-api";

import {
  ApplicationError,
  type ImportCommitPlan,
} from "../../application/index.js";
import type { Lot, RealizedRecord } from "../../domain/fifo/index.js";
import type { ParsedTransaction } from "../../domain/ledger/index.js";
import {
  completeMutationIdempotencyRecordInTransaction,
} from "./mutation-idempotency-repository.js";
import type { LyraDatabase } from "./database.js";

/** 执行一次已授权导入提交所需的事务前置条件。 */
export interface PersistImportCommitInput {
  plan: ImportCommitPlan;
  importId: string;
  accountId: string;
  parserVersion: string;
  previewFingerprint: string;
  expectedLedgerVersion: number;
  committedAt: Date;
  /** 存在幂等键时，在业务事务内把 in_progress 记录收口为 completed。 */
  idempotencyCompletion?: {
    recordId: string;
    resultResourceId: string;
    responseStatusCode: number;
  };
}

/** 导入提交完成后可返回应用层的稳定结果。 */
export interface PersistedImportCommitResult {
  changed: boolean;
  ledgerVersion: number;
  insertedRows: number;
  skippedRows: number;
}

/**
 * 在单一 DuckDB 事务中消费预览、写入交易并替换目标账户 FIFO 产物。
 *
 * 调用方必须通过 `WriteCoordinator` 串行调用本函数。事务内仍会比较账本版本、
 * 账户映射和导入状态，防止预览生成后队列等待期间发生数据变化。
 *
 * @param database - 已完成 v1–v10 迁移的单进程数据库。
 * @param input - 已验证的领域计划和预览持久化条件。
 */
export async function persistImportCommit(
  database: LyraDatabase,
  input: PersistImportCommitInput,
): Promise<PersistedImportCommitResult> {
  assertInput(input);
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    await assertAccountMatches(connection, input.accountId, input.plan.account);
    const currentLedgerVersion = await readCurrentLedgerVersion(connection);
    if (currentLedgerVersion !== input.expectedLedgerVersion) {
      throw new ApplicationError({
        code: "LEDGER_VERSION_CONFLICT",
        message: "账本版本已变化，必须重新生成导入预览",
        statusCode: 409,
        retryable: false,
        details: {
          expected_ledger_version: input.expectedLedgerVersion,
          actual_ledger_version: currentLedgerVersion,
        },
      });
    }
    await consumePreview(connection, input);

    if (!input.plan.changed) {
      await completeImportJob(connection, input, currentLedgerVersion);
      if (input.idempotencyCompletion !== undefined) {
        await completeMutationIdempotencyRecordInTransaction(
          connection,
          {
            recordId: input.idempotencyCompletion.recordId,
            resultResourceId: input.idempotencyCompletion.resultResourceId,
            responseStatusCode: input.idempotencyCompletion.responseStatusCode,
            completedAt: input.committedAt,
          },
        );
      }
      await connection.run("COMMIT");
      return {
        changed: false,
        ledgerVersion: currentLedgerVersion,
        insertedRows: 0,
        skippedRows: input.plan.skippedRows,
      };
    }
    if (input.plan.fifo === null) {
      throw new Error("变更导入计划缺少 FIFO 产物");
    }

    const ledgerVersion = await createLedgerVersion(connection, input.importId);
    // DuckDB 不允许更新已被外键引用的父行，即使主键本身不变；因此必须在
    // transactions 引用 import_files 之前写入作业终态。整个事务提交前不可见，
    // 后续事实或 FIFO 写入失败仍会连同该终态一起回滚。
    await completeImportJob(connection, input, ledgerVersion);
    for (const transaction of input.plan.insertedTransactions) {
      await insertTransaction(
        connection,
        transaction,
        input.accountId,
        input.importId,
        input.parserVersion,
        ledgerVersion,
      );
    }
    await connection.run(
      "DELETE FROM realized WHERE account_id = CAST($account_id AS UUID)",
      { account_id: input.accountId },
    );
    await connection.run(
      "DELETE FROM lots WHERE account_id = CAST($account_id AS UUID)",
      { account_id: input.accountId },
    );
    for (const lot of input.plan.fifo.openLots) {
      await insertLot(connection, lot, input.accountId, ledgerVersion);
    }
    for (const realized of input.plan.fifo.realizedRecords) {
      await insertRealized(connection, realized, input.accountId, ledgerVersion);
    }
    await connection.run(`
      INSERT INTO ledger_version_accounts (ledger_version, account_id)
      VALUES ($ledger_version, CAST($account_id AS UUID))
    `, { ledger_version: ledgerVersion, account_id: input.accountId });
    if (input.idempotencyCompletion !== undefined) {
      await completeMutationIdempotencyRecordInTransaction(
        connection,
        {
          recordId: input.idempotencyCompletion.recordId,
          resultResourceId: input.idempotencyCompletion.resultResourceId,
          responseStatusCode: input.idempotencyCompletion.responseStatusCode,
          completedAt: input.committedAt,
        },
      );
    }
    await connection.run("COMMIT");
    return {
      changed: true,
      ledgerVersion,
      insertedRows: input.plan.insertedRows,
      skippedRows: input.plan.skippedRows,
    };
  } catch (error: unknown) {
    try {
      await connection.run("ROLLBACK");
    } catch (rollbackError: unknown) {
      throw new AggregateError([error, rollbackError], "导入提交失败且事务回滚失败");
    }
    throw error;
  }
}

/** 校验不依赖数据库的调用边界。 */
function assertInput(input: PersistImportCommitInput): void {
  if (
    input.importId.trim().length === 0
    || input.accountId.trim().length === 0
    || input.parserVersion.trim().length === 0
    || !/^[a-f0-9]{64}$/u.test(input.previewFingerprint)
  ) {
    throw new RangeError("导入提交标识或预览指纹无效");
  }
  if (!Number.isSafeInteger(input.expectedLedgerVersion) || input.expectedLedgerVersion < 0) {
    throw new RangeError("预期账本版本必须是非负安全整数");
  }
  if (Number.isNaN(input.committedAt.getTime())) {
    throw new RangeError("导入提交时间无效");
  }
}

/** 账户 UUID 必须仍映射到计划中的 Schwab 旧键，且不可已归档。 */
async function assertAccountMatches(
  connection: DuckDBConnection,
  accountId: string,
  legacyAccountKey: string,
): Promise<void> {
  const reader = await connection.runAndReadAll(`
    SELECT legacy_account_key, status
    FROM accounts
    WHERE id = CAST($account_id AS UUID)
  `, { account_id: accountId });
  const row = reader.getRowObjectsJson()[0];
  if (
    row === undefined
    || row.legacy_account_key !== legacyAccountKey
    || row.status !== "active"
  ) {
    throw new ApplicationError({
      code: "ACCOUNT_MAPPING_REQUIRED",
      message: "导入目标账户已变化，请重新确认账户映射",
      statusCode: 409,
      retryable: false,
    });
  }
}

/** 读取当前全局账本版本；空账本使用 0。 */
async function readCurrentLedgerVersion(connection: DuckDBConnection): Promise<number> {
  const reader = await connection.runAndReadAll(
    "SELECT coalesce(max(version), 0) AS version FROM ledger_versions",
  );
  return parseVersion(reader.getRowObjectsJson()[0]?.version);
}

/** 以 compare-and-swap 一次性消费 preview_ready 作业。 */
async function consumePreview(
  connection: DuckDBConnection,
  input: PersistImportCommitInput,
): Promise<void> {
  const updated = await connection.run(`
    UPDATE import_files
    SET status = 'committing', updated_at = CAST($updated_at AS TIMESTAMPTZ)
    WHERE id = CAST($import_id AS UUID)
      AND status = 'preview_ready'
      AND target_account_id = CAST($account_id AS UUID)
      AND parser_version = $parser_version
      AND preview_fingerprint = $preview_fingerprint
      AND (
        preview_ledger_version = $expected_ledger_version
        OR (preview_ledger_version IS NULL AND $expected_ledger_version = 0)
      )
  `, {
    import_id: input.importId,
    account_id: input.accountId,
    parser_version: input.parserVersion,
    preview_fingerprint: input.previewFingerprint,
    expected_ledger_version: input.expectedLedgerVersion,
    updated_at: input.committedAt.toISOString(),
  });
  if (updated.rowsChanged !== 1) {
    throw new ApplicationError({
      code: "IMPORT_PREVIEW_STALE",
      message: "导入预览状态已变化，必须重新预览",
      statusCode: 409,
      retryable: false,
    });
  }
}

/** 为实际账本变更创建全局递增版本。 */
async function createLedgerVersion(
  connection: DuckDBConnection,
  importId: string,
): Promise<number> {
  const reader = await connection.runAndReadAll(`
    INSERT INTO ledger_versions (reason, source_reference)
    VALUES ('file_import', $source_reference)
    RETURNING version
  `, { source_reference: importId });
  return parseVersion(reader.getRowObjectsJson()[0]?.version);
}

/** 插入一个经过领域校验的标准交易事实。 */
async function insertTransaction(
  connection: DuckDBConnection,
  transaction: ParsedTransaction,
  accountId: string,
  importId: string,
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
      $source_type, $source_reference, CAST($import_file_id AS UUID), $source_hash,
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
    import_file_id: importId,
    source_hash: transaction.sourceHash,
    sequence: transaction.sequence,
    occurrence: transaction.occurrence,
    business_hash: transaction.businessHash,
    transaction_hash: transaction.transactionHash,
    parser_version: parserVersion,
    ledger_version: ledgerVersion,
  });
}

/** 插入一次重放后的未平 FIFO 批次。 */
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
      CAST($open_quantity AS DECIMAL(18, 6)),
      CAST($remaining_quantity AS DECIMAL(18, 6)),
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

/** 插入一次重放后的已实现损益记录。 */
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
      CAST($close_price AS DECIMAL(18, 6)), CAST($open_fees AS DECIMAL(18, 6)),
      CAST($close_fees AS DECIMAL(18, 6)), CAST($pnl AS DECIMAL(18, 4)),
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
    close_price: realized.closePrice.toString(),
    open_fees: realized.openFees.toString(),
    close_fees: realized.closeFees.toString(),
    pnl: realized.pnl.toString(),
    close_action: realized.closeAction,
    open_transaction_hash: realized.openTransaction,
    close_transaction_hash: realized.closeTransaction,
    ledger_version: ledgerVersion,
  });
}

/** 完成导入作业并清除已消费的预览授权摘要。 */
async function completeImportJob(
  connection: DuckDBConnection,
  input: PersistImportCommitInput,
  ledgerVersion: number,
): Promise<void> {
  const committedVersion = ledgerVersion === 0 ? null : ledgerVersion;
  const updated = await connection.run(`
    UPDATE import_files
    SET status = 'committed',
        total_rows = $total_rows,
        inserted_rows = $inserted_rows,
        skipped_rows = $skipped_rows,
        committed_ledger_version = $committed_ledger_version,
        preview_token_hash = NULL,
        preview_fingerprint = NULL,
        preview_expires_at = NULL,
        updated_at = CAST($updated_at AS TIMESTAMPTZ)
    WHERE id = CAST($import_id AS UUID) AND status = 'committing'
  `, {
    total_rows: input.plan.insertedRows + input.plan.skippedRows,
    inserted_rows: input.plan.insertedRows,
    skipped_rows: input.plan.skippedRows,
    committed_ledger_version: committedVersion,
    updated_at: input.committedAt.toISOString(),
    import_id: input.importId,
  });
  if (updated.rowsChanged !== 1) {
    throw new Error("导入作业提交终态更新失败");
  }
}

/** DuckDB BIGINT JSON 值只允许转换为安全非负版本。 */
function parseVersion(value: unknown): number {
  const parsed = typeof value === "number"
    ? value
    : typeof value === "string" && /^\d+$/u.test(value)
      ? Number(value)
      : Number.NaN;
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new RangeError("DuckDB 账本版本无效");
  }
  return parsed;
}
