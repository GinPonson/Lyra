import type { GmailConnectionRecord } from "../gmail/connection-credential.js";
import type { PersistedGmailAccountMappingRecord } from "../../application/gmail-account-mapping-view.js";
import { ApplicationError } from "../../application/app-error.js";
import { isGmailSyncErrorCode } from "../../domain/gmail/index.js";
import type { LyraDatabase } from "./database.js";

/**
 * 把 DuckDB TIMESTAMPTZ 文本（如 "2026-09-05 10:08:23.879941+08"）规范为保留
 * 微秒的 UTC ISO 文本。
 *
 * 微秒精度必须保留：连接状态的乐观锁以 updated_at 精确相等做比较，经 Date
 * 归一化会丢失微秒并使所有基于 current_timestamp 的行永远无法通过 CAS。
 */
function parseTimestamp(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new Error(`${label}必须是时间字符串`);
  }
  const match = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(?:([+-])(\d{2})(?::?(\d{2}))?|Z|UTC)?$/.exec(
    value.trim(),
  );
  if (match === null) {
    throw new Error(`${label}无效`);
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const fraction = match[7];
  const sign = match[8];
  const offsetHour = match[9] === undefined ? 0 : Number(match[9]);
  const offsetMinute = match[10] === undefined ? 0 : Number(match[10]);
  const offsetMinutes = sign === undefined || sign === "+"
    ? offsetHour * 60 + offsetMinute
    : -(offsetHour * 60 + offsetMinute);
  const utcMillis = Date.UTC(year, month - 1, day, hour, minute, second)
    - offsetMinutes * 60_000;
  const base = new Date(utcMillis);
  if (Number.isNaN(base.getTime())) {
    throw new Error(`${label}无效`);
  }
  const micros = (fraction ?? "0").padEnd(6, "0").slice(0, 6);
  return base.toISOString().replace(/\.\d{3}Z$/, `.${micros}Z`);
}

function requireText(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${label}与数据库 schema 不一致`);
  }
  return value;
}

function parseConnectionRow(row: Readonly<Record<string, unknown>>): GmailConnectionRecord {
  const status = row.status;
  if (
    status !== "pending_initial_scan" && status !== "active"
    && status !== "needs_resync" && status !== "disconnected" && status !== "error"
  ) {
    throw new Error("Gmail 连接状态与数据库 schema 不一致");
  }
  const lastErrorCode = row.last_error_code === null
    ? null
    : requireText(row.last_error_code, "Gmail 连接错误码");
  if (lastErrorCode !== null && !isGmailSyncErrorCode(lastErrorCode)) {
    throw new Error("Gmail 连接错误码与稳定闭集不一致");
  }
  return {
    id: requireText(row.id, "Gmail 连接 ID"),
    googleSubject: requireText(row.google_subject, "Google subject"),
    googleEmail: requireText(row.google_email, "Google 邮箱"),
    encryptedRefreshToken: row.encrypted_refresh_token === null
      ? null
      : requireText(row.encrypted_refresh_token, "Gmail refresh token 密文"),
    grantedScopes: requireText(row.granted_scopes, "Gmail 授权 scope"),
    historyId: row.history_id === null ? null : requireText(row.history_id, "Gmail history 游标"),
    status,
    lastErrorCode,
    lastSyncAt: row.last_sync_at === null
      ? null
      : parseTimestamp(row.last_sync_at, "Gmail 最近同步时间"),
    createdAt: parseTimestamp(row.created_at, "Gmail 连接创建时间"),
    updatedAt: parseTimestamp(row.updated_at, "Gmail 连接更新时间"),
  };
}

/** 持久化一条新建的 pending_initial_scan 连接；重复 subject 被唯一约束拒绝。 */
export async function createGmailConnectionRow(
  database: LyraDatabase,
  record: GmailConnectionRecord,
): Promise<void> {
  await database.connection.run(`
    INSERT INTO gmail_connections (
      id, google_subject, google_email, encrypted_refresh_token,
      granted_scopes, history_id, status, last_error_code, last_sync_at,
      created_at, updated_at
    ) VALUES (
      CAST($id AS UUID), $google_subject, $google_email, $encrypted_refresh_token,
      $granted_scopes, $history_id, $status, $last_error_code, $last_sync_at,
      CAST($created_at AS TIMESTAMPTZ), CAST($updated_at AS TIMESTAMPTZ)
    )
  `, {
    id: record.id,
    google_subject: record.googleSubject,
    google_email: record.googleEmail,
    encrypted_refresh_token: record.encryptedRefreshToken,
    granted_scopes: record.grantedScopes,
    history_id: record.historyId,
    status: record.status,
    last_error_code: record.lastErrorCode,
    last_sync_at: record.lastSyncAt,
    created_at: record.createdAt,
    updated_at: record.updatedAt,
  });
}

/** 加载全部 Gmail 连接，按创建时间和 ID 稳定排序。 */
export async function loadGmailConnectionRows(
  database: LyraDatabase,
): Promise<GmailConnectionRecord[]> {
  const reader = await database.connection.runAndReadAll(`
    SELECT
      CAST(id AS VARCHAR) AS id,
      google_subject,
      google_email,
      encrypted_refresh_token,
      granted_scopes,
      history_id,
      status,
      last_error_code,
      last_sync_at,
      created_at,
      updated_at
    FROM gmail_connections
    ORDER BY created_at, id
  `);
  return reader.getRowObjectsJson().map(parseConnectionRow);
}

/** 按 ID 加载单条连接；不存在返回 null。 */
export async function loadGmailConnectionRow(
  database: LyraDatabase,
  connectionId: string,
): Promise<GmailConnectionRecord | null> {
  const reader = await database.connection.runAndReadAll(`
    SELECT
      CAST(id AS VARCHAR) AS id,
      google_subject,
      google_email,
      encrypted_refresh_token,
      granted_scopes,
      history_id,
      status,
      last_error_code,
      last_sync_at,
      created_at,
      updated_at
    FROM gmail_connections
    WHERE id = CAST($id AS UUID)
  `, { id: connectionId });
  const row = reader.getRowObjectsJson()[0];
  return row === undefined ? null : parseConnectionRow(row);
}

/** 以原 updated_at 做比较并交换保存连接状态；并发更新返回冲突。 */
export async function saveGmailConnectionRow(
  database: LyraDatabase,
  record: GmailConnectionRecord,
  expectedUpdatedAt: string,
): Promise<void> {
  const updated = await database.connection.run(`
    UPDATE gmail_connections
    SET encrypted_refresh_token = $encrypted_refresh_token,
        granted_scopes = $granted_scopes,
        history_id = $history_id,
        status = $status,
        last_error_code = $last_error_code,
        last_sync_at = $last_sync_at,
        updated_at = CAST($updated_at AS TIMESTAMPTZ)
    WHERE id = CAST($id AS UUID)
      AND updated_at = CAST($expected_updated_at AS TIMESTAMPTZ)
  `, {
    id: record.id,
    encrypted_refresh_token: record.encryptedRefreshToken,
    granted_scopes: record.grantedScopes,
    history_id: record.historyId,
    status: record.status,
    last_error_code: record.lastErrorCode,
    last_sync_at: record.lastSyncAt,
    updated_at: record.updatedAt,
    expected_updated_at: expectedUpdatedAt,
  });
  if (updated.rowsChanged !== 1) {
    throw new ApplicationError({
      code: "GMAIL_CONNECTION_ERROR",
      message: "Gmail 连接或映射已变化，请刷新后重试",
      statusCode: 409,
      retryable: true,
    });
  }
}

/** 加载一个连接的映射列表，按尾号与 ID 稳定排序。 */
export async function loadGmailAccountMappingRows(
  database: LyraDatabase,
  connectionId: string,
): Promise<PersistedGmailAccountMappingRecord[]> {
  const reader = await database.connection.runAndReadAll(`
    SELECT
      CAST(id AS VARCHAR) AS id,
      CAST(connection_id AS VARCHAR) AS connection_id,
      account_suffix,
      CAST(account_id AS VARCHAR) AS account_id,
      created_at,
      updated_at
    FROM gmail_account_mappings
    WHERE connection_id = CAST($connection_id AS UUID)
    ORDER BY account_suffix, id
  `, { connection_id: connectionId });
  return reader.getRowObjectsJson().map((row) => ({
    id: requireText(row.id, "Gmail 映射 ID"),
    connectionId: requireText(row.connection_id, "Gmail 映射连接 ID"),
    accountSuffix: requireText(row.account_suffix, "Gmail 账户尾号"),
    accountId: requireText(row.account_id, "Gmail 映射账户 ID"),
    createdAt: parseTimestamp(row.created_at, "Gmail 映射创建时间"),
    updatedAt: parseTimestamp(row.updated_at, "Gmail 映射更新时间"),
  }));
}

/** 按映射 ID 读取单条映射；不存在返回 null。 */
export async function loadGmailAccountMappingRow(
  database: LyraDatabase,
  mappingId: string,
): Promise<PersistedGmailAccountMappingRecord | null> {
  const reader = await database.connection.runAndReadAll(`
    SELECT
      CAST(id AS VARCHAR) AS id,
      CAST(connection_id AS VARCHAR) AS connection_id,
      account_suffix,
      CAST(account_id AS VARCHAR) AS account_id,
      created_at,
      updated_at
    FROM gmail_account_mappings
    WHERE id = CAST($mapping_id AS UUID)
  `, { mapping_id: mappingId });
  const row = reader.getRowObjectsJson()[0];
  if (row === undefined) {
    return null;
  }
  return {
    id: requireText(row.id, "Gmail 映射 ID"),
    connectionId: requireText(row.connection_id, "Gmail 映射连接 ID"),
    accountSuffix: requireText(row.account_suffix, "Gmail 账户尾号"),
    accountId: requireText(row.account_id, "Gmail 映射账户 ID"),
    createdAt: parseTimestamp(row.created_at, "Gmail 映射创建时间"),
    updatedAt: parseTimestamp(row.updated_at, "Gmail 映射更新时间"),
  };
}

/** 插入同步流程首次确认的映射；尾号唯一约束冲突由调用方收口。 */
export async function createGmailAccountMappingRow(
  database: LyraDatabase,
  record: { connectionId: string; accountSuffix: string; accountId: string; at: string },
): Promise<PersistedGmailAccountMappingRecord> {
  const reader = await database.connection.runAndReadAll(`
    INSERT INTO gmail_account_mappings (
      id, connection_id, account_suffix, account_id, created_at, updated_at
    ) VALUES (
      CAST($id AS UUID), CAST($connection_id AS UUID), $account_suffix,
      CAST($account_id AS UUID),
      CAST($at AS TIMESTAMPTZ), CAST($at AS TIMESTAMPTZ)
    )
    RETURNING id, created_at, updated_at
  `, {
    id: randomMappingId(),
    connection_id: record.connectionId,
    account_suffix: record.accountSuffix,
    account_id: record.accountId,
    at: record.at,
  });
  const row = reader.getRowObjectsJson()[0];
  return {
    id: requireText(row?.id, "Gmail 映射 ID"),
    connectionId: record.connectionId,
    accountSuffix: record.accountSuffix,
    accountId: record.accountId,
    createdAt: parseTimestamp(row?.created_at, "Gmail 映射创建时间"),
    updatedAt: parseTimestamp(row?.updated_at, "Gmail 映射更新时间"),
  };
}

/** 以旧目标账户做比较并交换完成映射改绑；并发改动返回冲突。 */
export async function updateGmailAccountMappingRow(
  database: LyraDatabase,
  input: {
    mappingId: string;
    expectedAccountId: string;
    nextAccountId: string;
    at: string;
  },
): Promise<void> {
  const updated = await database.connection.run(`
    UPDATE gmail_account_mappings
    SET account_id = CAST($next_account_id AS UUID),
        updated_at = CAST($at AS TIMESTAMPTZ)
    WHERE id = CAST($mapping_id AS UUID)
      AND account_id = CAST($expected_account_id AS UUID)
  `, {
    mapping_id: input.mappingId,
    next_account_id: input.nextAccountId,
    expected_account_id: input.expectedAccountId,
    at: input.at,
  });
  if (updated.rowsChanged !== 1) {
    throw new ApplicationError({
      code: "GMAIL_CONNECTION_ERROR",
      message: "Gmail 映射已变化，请刷新后重试",
      statusCode: 409,
      retryable: true,
    });
  }
}

function randomMappingId(): string {
  return globalThis.crypto.randomUUID();
}
