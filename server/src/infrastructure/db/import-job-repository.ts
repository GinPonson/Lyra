import type {
  ImportFileType,
} from "../../application/import-upload.js";
import type { ImportJobStatus } from "../../application/import-job.js";
import type { PersistedImportJobRecord } from "../../application/import-job-view.js";
import { ApplicationError } from "../../application/app-error.js";
import type { LyraDatabase } from "./database.js";

/** 导入账户映射需要的最小账户投影。 */
export interface ImportAccountRow {
  id: string;
  legacyAccountKey: string;
  maskedSuffix: string;
  status: "active" | "archived";
}

/** 与 v7 预览授权列对齐的持久化记录；数据库只保存令牌哈希。 */
export interface ImportPreviewAuthorizationRow {
  importId: string;
  previewFingerprint: string;
  tokenHash: string | null;
  expiresAt: string | null;
}

/** 创建 uploaded 状态的导入作业记录；目标账户在解析映射后随预览落库。 */
export async function createImportJobRecord(
  database: LyraDatabase,
  input: {
    importId: string;
    fileHash: string;
    fileName: string;
    fileType: ImportFileType;
    fileSizeBytes: number;
    parserVersion: string;
    createdAt: string;
  },
): Promise<void> {
  await database.connection.run(`
    INSERT INTO import_files (
      id, file_hash, original_file_name, file_type, file_size_bytes,
      parser_version, status, target_account_id,
      created_at, updated_at
    ) VALUES (
      CAST($import_id AS UUID), $file_hash, $file_name, $file_type,
      $file_size_bytes, $parser_version, 'uploaded',
      NULL,
      CAST($created_at AS TIMESTAMPTZ), CAST($created_at AS TIMESTAMPTZ)
    )
  `, {
    import_id: input.importId,
    file_hash: input.fileHash,
    file_name: input.fileName,
    file_type: input.fileType,
    file_size_bytes: input.fileSizeBytes,
    parser_version: input.parserVersion,
    created_at: input.createdAt,
  });
}

/** 以 CAS 更新导入作业状态；并发冲突或终态回退统一返回稳定冲突。 */
export async function casImportJobStatus(
  database: LyraDatabase,
  input: {
    importId: string;
    from: ImportJobStatus;
    to: ImportJobStatus;
    at: string;
    errorCode?: string | null;
    errorSummary?: string | null;
  },
): Promise<void> {
  const updated = await database.connection.run(`
    UPDATE import_files
    SET status = $to_status,
        error_code = $error_code,
        error_summary = $error_summary,
        updated_at = CAST($updated_at AS TIMESTAMPTZ)
    WHERE id = CAST($import_id AS UUID)
      AND status = $from_status
  `, {
    import_id: input.importId,
    from_status: input.from,
    to_status: input.to,
    error_code: input.errorCode ?? null,
    error_summary: input.errorSummary ?? null,
    updated_at: input.at,
  });
  if (updated.rowsChanged !== 1) {
    throw new ApplicationError({
      code: "IMPORT_STATE_CONFLICT",
      message: "导入作业状态已变化",
      statusCode: 409,
      retryable: false,
    });
  }
}

/** 把解析完成的预览字段与一次性确认令牌哈希随 preview_ready 一起落库。 */
export async function saveImportPreview(
  database: LyraDatabase,
  input: {
    importId: string;
    accountId: string;
    fingerprint: string;
    expiresAt: string;
    tokenHash: string;
    /** 空账本为 null（版本 0 不存在对应行），提交仓储按同一约定解释。 */
    previewLedgerVersion: number | null;
    totalRows: number;
    insertedRows: number;
    skippedRows: number;
    at: string;
  },
): Promise<void> {
  const updated = await database.connection.run(`
    UPDATE import_files
    SET status = 'preview_ready',
        target_account_id = CAST($account_id AS UUID),
        preview_fingerprint = $fingerprint,
        preview_expires_at = CAST($expires_at AS TIMESTAMPTZ),
        preview_token_hash = $token_hash,
        preview_ledger_version = $preview_ledger_version,
        total_rows = $total_rows,
        inserted_rows = $inserted_rows,
        skipped_rows = $skipped_rows,
        updated_at = CAST($updated_at AS TIMESTAMPTZ)
    WHERE id = CAST($import_id AS UUID)
      AND status = 'parsing'
  `, {
    import_id: input.importId,
    account_id: input.accountId,
    fingerprint: input.fingerprint,
    expires_at: input.expiresAt,
    token_hash: input.tokenHash,
    preview_ledger_version: input.previewLedgerVersion,
    total_rows: input.totalRows,
    inserted_rows: input.insertedRows,
    skipped_rows: input.skippedRows,
    updated_at: input.at,
  });
  if (updated.rowsChanged !== 1) {
    throw new ApplicationError({
      code: "IMPORT_STATE_CONFLICT",
      message: "导入作业状态已变化，预览结果未生效",
      statusCode: 409,
      retryable: false,
    });
  }
}

/** 读取导入作业完整记录；不存在返回 null。 */
export async function loadImportJobRecord(
  database: LyraDatabase,
  importId: string,
): Promise<PersistedImportJobRecord | null> {
  const reader = await database.connection.runAndReadAll(`
    SELECT
      CAST(id AS VARCHAR) AS id,
      file_hash,
      original_file_name,
      file_type,
      file_size_bytes,
      parser_version,
      status,
      CAST(target_account_id AS VARCHAR) AS target_account_id,
      preview_token_hash,
      preview_ledger_version,
      total_rows,
      inserted_rows,
      skipped_rows,
      error_code,
      error_summary,
      committed_ledger_version,
      created_at,
      updated_at
    FROM import_files
    WHERE id = CAST($import_id AS UUID)
  `, { import_id: importId });
  const row = reader.getRowObjectsJson()[0];
  if (row === undefined) {
    return null;
  }
  return {
    id: requireText(row.id, "导入作业 ID"),
    fileHash: requireText(row.file_hash, "导入文件哈希"),
    originalFileName: requireText(row.original_file_name, "导入文件名"),
    fileType: row.file_type === "csv" || row.file_type === "xlsx"
      ? row.file_type
      : (() => {
        throw new Error("导入文件类型与数据库 schema 不一致");
      })(),
    fileSizeBytes: Number(row.file_size_bytes),
    parserVersion: requireText(row.parser_version, "导入解析器版本"),
    status: row.status as ImportJobStatus,
    targetAccountId: row.target_account_id === null
      ? null
      : requireText(row.target_account_id, "导入目标账户"),
    previewTokenHash: row.preview_token_hash === null
      ? null
      : requireText(row.preview_token_hash, "导入预览令牌哈希"),
    previewLedgerVersion: row.preview_ledger_version === null
      ? null
      : Number(row.preview_ledger_version),
    totalRows: row.total_rows === null ? null : Number(row.total_rows),
    insertedRows: row.inserted_rows === null ? null : Number(row.inserted_rows),
    skippedRows: row.skipped_rows === null ? null : Number(row.skipped_rows),
    errorCode: row.error_code === null ? null : requireText(row.error_code, "导入错误码"),
    errorSummary: row.error_summary === null
      ? null
      : requireText(row.error_summary, "导入错误摘要"),
    committedLedgerVersion: row.committed_ledger_version === null
      ? null
      : Number(row.committed_ledger_version),
    createdAt: parseTimestamp(row.created_at, "导入创建时间"),
    updatedAt: parseTimestamp(row.updated_at, "导入更新时间"),
  };
}

/** 读取提交确认所需的预览授权列；未完成预览返回 null。 */
export async function loadImportPreviewAuthorization(
  database: LyraDatabase,
  importId: string,
): Promise<ImportPreviewAuthorizationRow | null> {
  const reader = await database.connection.runAndReadAll(`
    SELECT
      CAST(id AS VARCHAR) AS import_id,
      preview_fingerprint,
      preview_token_hash,
      preview_expires_at
    FROM import_files
    WHERE id = CAST($import_id AS UUID)
  `, { import_id: importId });
  const row = reader.getRowObjectsJson()[0];
  if (row === undefined || row.preview_fingerprint === null) {
    return null;
  }
  return {
    importId: requireText(row.import_id, "导入作业 ID"),
    previewFingerprint: requireText(row.preview_fingerprint, "导入预览指纹"),
    tokenHash: row.preview_token_hash === null
      ? null
      : requireText(row.preview_token_hash, "导入预览令牌哈希"),
    expiresAt: row.preview_expires_at === null
      ? null
      : parseTimestamp(row.preview_expires_at, "导入预览有效期"),
  };
}

/** 加载导入账户映射需要的最小账户列表。 */
export async function loadImportAccounts(
  database: LyraDatabase,
): Promise<ImportAccountRow[]> {
  const reader = await database.connection.runAndReadAll(`
    SELECT
      CAST(id AS VARCHAR) AS id,
      legacy_account_key,
      masked_suffix,
      status
    FROM accounts
    ORDER BY created_at, id
  `);
  return reader.getRowObjectsJson().map((row) => ({
    id: requireText(row.id, "账户 ID"),
    legacyAccountKey: requireText(row.legacy_account_key, "账户旧键"),
    maskedSuffix: requireText(row.masked_suffix, "账户脱敏后缀"),
    status: row.status === "active" || row.status === "archived"
      ? row.status
      : (() => {
        throw new Error("账户状态与数据库 schema 不一致");
      })(),
  }));
}

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

function requireText(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${label}与数据库 schema 不一致`);
  }
  return value;
}
