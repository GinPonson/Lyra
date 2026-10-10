import type { DuckDBConnection } from "@duckdb/node-api";

import type { MutationIdempotencyRecord } from "../../application/index.js";
import type { LyraDatabase } from "./database.js";

/** 仓储读取行与领域记录之间的最小必要校验。 */
function requireText(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${label}无效`);
  }
  return value;
}

function requireCount(value: unknown, label: string): number {
  const parsed = typeof value === "number"
    ? value
    : typeof value === "string" && /^\d+$/u.test(value)
      ? Number(value)
      : Number.NaN;
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new Error(`${label}无效`);
  }
  return parsed;
}

/** DuckDB TIMESTAMPTZ 文本统一规范为 UTC ISO 字符串。 */
function parseTimestamp(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new Error(`${label}必须是时间字符串`);
  }
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) {
    throw new Error(`${label}无效`);
  }
  return timestamp.toISOString();
}

/** 把 DuckDB 行转换为领域幂等记录；时间统一读取为 UTC ISO 文本。 */
function mapRecord(row: Record<string, unknown>): MutationIdempotencyRecord {
  return {
    id: requireText(row.id, "幂等记录 ID"),
    deviceId: requireText(row.device_id, "幂等设备 ID"),
    operation: requireText(row.operation, "幂等操作名"),
    keyHash: requireText(row.key_hash, "幂等键哈希"),
    requestHash: requireText(row.request_hash, "幂等请求指纹"),
    status: row.status === "in_progress" || row.status === "completed"
      ? row.status
      : (() => {
        throw new Error("幂等记录状态与数据库 schema 不一致");
      })(),
    resultResourceId: typeof row.result_resource_id === "string"
      && row.result_resource_id.length > 0
      ? row.result_resource_id
      : null,
    responseStatusCode: row.response_status_code === null
      ? null
      : requireCount(row.response_status_code, "幂等响应状态"),
    createdAt: parseTimestamp(row.created_at, "幂等记录创建时间"),
    expiresAt: parseTimestamp(row.expires_at, "幂等记录过期时间"),
    completedAt: row.completed_at === null
      ? null
      : parseTimestamp(row.completed_at, "幂等记录完成时间"),
  };
}

/**
 * 按设备、操作与键哈希读取唯一幂等记录；不存在返回 null。
 *
 * 过期记录仍会返回，由领域 `planIdempotentMutation` 判断过期替换策略。
 */
export async function loadMutationIdempotencyRecord(
  database: LyraDatabase,
  input: { deviceId: string; operation: string; keyHash: string },
): Promise<MutationIdempotencyRecord | null> {
  const reader = await database.connection.runAndReadAll(`
    SELECT
      CAST(id AS VARCHAR) AS id,
      CAST(device_id AS VARCHAR) AS device_id,
      operation,
      key_hash,
      request_hash,
      status,
      result_resource_id,
      response_status_code,
      CAST(created_at AS VARCHAR) AS created_at,
      CAST(expires_at AS VARCHAR) AS expires_at,
      CAST(completed_at AS VARCHAR) AS completed_at
    FROM mutation_idempotency_records
    WHERE device_id = CAST($device_id AS UUID)
      AND operation = $operation
      AND key_hash = $key_hash
  `, {
    device_id: input.deviceId,
    operation: input.operation,
    key_hash: input.keyHash,
  });
  const row = reader.getRowObjectsJson()[0];
  return row === undefined ? null : mapRecord(row);
}

/**
 * 以单一事务开始一条 in_progress 幂等记录；必要时原子替换过期旧记录。
 *
 * 唯一约束 `(device_id, operation, key_hash)` 保证并发同键请求只会成功一条；
 * 后续重复键在业务开始前就能读取到 in_progress 状态。
 */
export async function startMutationIdempotencyRecord(
  database: LyraDatabase,
  input: {
    record: MutationIdempotencyRecord;
    replacesExpiredRecordId: string | null;
  },
): Promise<void> {
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    await startMutationIdempotencyRecordInTransaction(connection, input);
    await connection.run("COMMIT");
  } catch (error: unknown) {
    try {
      await connection.run("ROLLBACK");
    } catch (rollbackError: unknown) {
      throw new AggregateError([error, rollbackError], "幂等记录开始失败且事务回滚失败");
    }
    throw error;
  }
}

/** 在调用方业务事务内开始幂等记录，支持短操作与业务结果一起原子提交。 */
export async function startMutationIdempotencyRecordInTransaction(
  connection: DuckDBConnection,
  input: {
    record: MutationIdempotencyRecord;
    replacesExpiredRecordId: string | null;
  },
): Promise<void> {
  if (input.replacesExpiredRecordId !== null) {
    await connection.run(`
      DELETE FROM mutation_idempotency_records
      WHERE id = CAST($record_id AS UUID)
    `, { record_id: input.replacesExpiredRecordId });
  }
  await connection.run(`
    INSERT INTO mutation_idempotency_records (
      id, device_id, operation, key_hash, request_hash, status,
      result_resource_id, response_status_code,
      created_at, expires_at, completed_at
    ) VALUES (
      CAST($id AS UUID), CAST($device_id AS UUID), $operation, $key_hash,
      $request_hash, 'in_progress', NULL, NULL,
      CAST($created_at AS TIMESTAMPTZ), CAST($expires_at AS TIMESTAMPTZ), NULL
    )
  `, {
    id: input.record.id,
    device_id: input.record.deviceId,
    operation: input.record.operation,
    key_hash: input.record.keyHash,
    request_hash: input.record.requestHash,
    created_at: input.record.createdAt,
    expires_at: input.record.expiresAt,
  });
}

/**
 * 在调用方业务事务内把 in_progress 记录 CAS 收口为 completed。
 *
 * 必须传入与业务写入共享的连接；任何失败都会随业务事务一起回滚，
 * 保证"幂等完成"与"业务结果可见"同生共死。
 */
export async function completeMutationIdempotencyRecordInTransaction(
  connection: DuckDBConnection,
  input: {
    recordId: string;
    resultResourceId: string;
    responseStatusCode: number;
    completedAt: Date;
  },
): Promise<void> {
  if (Number.isNaN(input.completedAt.getTime())) {
    throw new Error("幂等完成时间无效");
  }
  const updated = await connection.run(`
    UPDATE mutation_idempotency_records
    SET status = 'completed',
        result_resource_id = $result_resource_id,
        response_status_code = $response_status_code,
        completed_at = CAST($completed_at AS TIMESTAMPTZ)
    WHERE id = CAST($record_id AS UUID)
      AND status = 'in_progress'
  `, {
    record_id: input.recordId,
    result_resource_id: input.resultResourceId,
    response_status_code: input.responseStatusCode,
    completed_at: input.completedAt.toISOString(),
  });
  if (updated.rowsChanged !== 1) {
    throw new Error("幂等记录完成收口失败");
  }
}

/**
 * 业务失败时释放仍为 in_progress 的幂等记录，让客户端可以用同一键重试。
 *
 * 只删除 in_progress 行；已完成记录是重放依据，永远不会被释放路径触碰。
 */
export async function releaseMutationIdempotencyRecord(
  database: LyraDatabase,
  recordId: string,
): Promise<void> {
  await database.connection.run(`
    DELETE FROM mutation_idempotency_records
    WHERE id = CAST($record_id AS UUID)
      AND status = 'in_progress'
  `, { record_id: recordId });
}
