import { ApplicationError } from "../../application/app-error.js";
import type { IdempotentMutationPlan } from "../../application/idempotent-mutation.js";
import type { BrokerageAccount } from "../../domain/accounts/index.js";
import type { AuditEvent } from "../../domain/audit/index.js";
import type { LyraDatabase } from "./database.js";
import {
  completeMutationIdempotencyRecordInTransaction,
  startMutationIdempotencyRecordInTransaction,
} from "./mutation-idempotency-repository.js";

/** 幂等重放只按已授权设备记录的账户 UUID 读取，不接受客户端指定结果。 */
export async function loadBrokerageAccount(
  database: LyraDatabase,
  accountId: string,
): Promise<BrokerageAccount | null> {
  const reader = await database.connection.runAndReadAll(`
    SELECT CAST(id AS VARCHAR) AS id, display_name, legacy_account_key, broker,
      masked_suffix, base_currency, status,
      CAST(created_at AS VARCHAR) AS created_at,
      CAST(updated_at AS VARCHAR) AS updated_at
    FROM accounts WHERE id = CAST($id AS UUID)
  `, { id: accountId });
  const row = reader.getRowObjectsJson()[0];
  if (row === undefined) {
    return null;
  }
  if (row.broker !== "schwab" || (row.status !== "active" && row.status !== "archived")) {
    throw new Error("账户记录状态无效");
  }
  return {
    id: requireText(row.id),
    displayName: requireText(row.display_name),
    legacyAccountKey: requireText(row.legacy_account_key),
    broker: row.broker,
    maskedSuffix: requireText(row.masked_suffix),
    baseCurrency: requireText(row.base_currency),
    status: row.status,
    createdAt: new Date(requireText(row.created_at)).toISOString(),
    updatedAt: new Date(requireText(row.updated_at)).toISOString(),
  };
}

/**
 * 账户、最小审计事件与幂等结果在同一事务中落库。
 *
 * 调用方必须持有共享 WriteCoordinator 的队列位置。账户只是元数据，不创建
 * ledger_versions、虚构交易或 FIFO 批次；大小写等价的历史标识也不能重复创建。
 */
export async function persistCreatedBrokerageAccount(
  database: LyraDatabase,
  input: {
    account: BrokerageAccount;
    auditEvent: AuditEvent;
    idempotency: Extract<IdempotentMutationPlan, { kind: "start" }> | null;
  },
): Promise<void> {
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    const duplicates = await connection.runAndReadAll(`
      SELECT 1 FROM accounts
      WHERE upper(trim(legacy_account_key)) = $identifier LIMIT 1
    `, { identifier: input.account.legacyAccountKey.toUpperCase() });
    if (duplicates.getRowObjectsJson().length > 0) {
      throw new ApplicationError({
        code: "ACCOUNT_ALREADY_EXISTS",
        message: "该文件账户标识已存在，请选择已有账户；已归档账户也不能重复创建",
        statusCode: 409,
        retryable: false,
      });
    }
    if (input.idempotency !== null) {
      await startMutationIdempotencyRecordInTransaction(connection, input.idempotency);
    }
    const account = input.account;
    await connection.run(`
      INSERT INTO accounts (
        id, display_name, legacy_account_key, broker, masked_suffix,
        base_currency, status, created_at, updated_at
      ) VALUES (
        CAST($id AS UUID), $display_name, $legacy_account_key, $broker, $masked_suffix,
        $base_currency, $status, CAST($created_at AS TIMESTAMPTZ), CAST($updated_at AS TIMESTAMPTZ)
      )
    `, {
      id: account.id,
      display_name: account.displayName,
      legacy_account_key: account.legacyAccountKey,
      broker: account.broker,
      masked_suffix: account.maskedSuffix,
      base_currency: account.baseCurrency,
      status: account.status,
      created_at: account.createdAt,
      updated_at: account.updatedAt,
    });
    const event = input.auditEvent;
    await connection.run(`
      INSERT INTO audit_events (
        id, occurred_at, event_type, outcome, actor_type, actor_id, request_id,
        target_type, target_id, account_id, ledger_version, error_code, metadata_json
      ) VALUES (
        CAST($id AS UUID), CAST($occurred_at AS TIMESTAMPTZ), $event_type, $outcome,
        $actor_type, CAST($actor_id AS UUID), $request_id, $target_type, $target_id,
        CAST($account_id AS UUID), NULL, NULL, CAST($metadata_json AS JSON)
      )
    `, {
      id: event.id,
      occurred_at: event.occurredAt,
      event_type: event.eventType,
      outcome: event.outcome,
      actor_type: event.actorType,
      actor_id: event.actorId,
      request_id: event.requestId,
      target_type: event.targetType,
      target_id: event.targetId,
      account_id: event.accountId,
      metadata_json: JSON.stringify(event.metadata),
    });
    if (input.idempotency !== null) {
      await completeMutationIdempotencyRecordInTransaction(connection, {
        recordId: input.idempotency.record.id,
        resultResourceId: account.id,
        responseStatusCode: 201,
        completedAt: new Date(account.createdAt),
      });
    }
    await connection.run("COMMIT");
  } catch (error: unknown) {
    try {
      await connection.run("ROLLBACK");
    } catch (rollbackError: unknown) {
      throw new AggregateError([error, rollbackError], "账户创建事务失败且回滚失败");
    }
    throw error;
  }
}

function requireText(value: unknown): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error("账户记录字段无效");
  }
  return value;
}
