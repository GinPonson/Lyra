import type { ApiErrorCode } from "./app-error.js";
import type { ImportJobStatus } from "./import-job.js";

/** 导入作业允许持久化并公开的稳定错误码。 */
export const IMPORT_JOB_ERROR_CODES = [
  "IMPORT_UNSUPPORTED_FORMAT",
  "IMPORT_UNKNOWN_HEADERS",
  "IMPORT_AMBIGUOUS_SHEET",
  "IMPORT_VALIDATION_FAILED",
  "IMPORT_PREVIEW_STALE",
  "IMPORT_STATE_CONFLICT",
  "LEDGER_VERSION_CONFLICT",
  "LEDGER_REBUILD_FAILED",
  "INTERNAL_ERROR",
] as const satisfies readonly ApiErrorCode[];

/** App 可稳定分支处理的导入作业错误码。 */
export type ImportJobErrorCode = typeof IMPORT_JOB_ERROR_CODES[number];

/** 与 DuckDB `import_files` 对齐的完整导入作业记录。 */
export interface PersistedImportJobRecord {
  id: string;
  fileHash: string;
  originalFileName: string;
  fileType: "csv" | "xlsx";
  fileSizeBytes: number;
  parserVersion: string;
  status: ImportJobStatus;
  targetAccountId: string | null;
  previewTokenHash: string | null;
  previewLedgerVersion: number | null;
  totalRows: number | null;
  insertedRows: number | null;
  skippedRows: number | null;
  errorCode: string | null;
  errorSummary: string | null;
  committedLedgerVersion: number | null;
  createdAt: string;
  updatedAt: string;
}

/** App 恢复导入流程所需的最小公开作业状态。 */
export interface PublicImportJob {
  id: string;
  fileName: string;
  fileType: "csv" | "xlsx";
  fileSizeBytes: number;
  status: ImportJobStatus;
  targetAccountId: string | null;
  baseLedgerVersion: number | null;
  totalRows: number | null;
  insertedRows: number | null;
  skippedRows: number | null;
  errorCode: ImportJobErrorCode | null;
  committedLedgerVersion: number | null;
  createdAt: string;
  updatedAt: string;
}

/**
 * 将完整导入记录投影为 App 可安全轮询和恢复的状态。
 *
 * 文件哈希、解析器版本、预览令牌摘要和底层错误详情只用于服务端提交校验与
 * 日志。数据库中的非稳定错误码会被明确拒绝，不能作为自由文本透传给 App。
 *
 * @param job - 仓储读取的完整导入作业记录。
 * @returns 不包含提交秘密与内部错误详情的公开状态。
 */
export function createPublicImportJob(job: PersistedImportJobRecord): PublicImportJob {
  if (job.errorCode !== null && !isImportJobErrorCode(job.errorCode)) {
    throw new RangeError(`导入作业包含非稳定错误码: ${job.errorCode}`);
  }
  return {
    id: job.id,
    fileName: job.originalFileName,
    fileType: job.fileType,
    fileSizeBytes: job.fileSizeBytes,
    status: job.status,
    targetAccountId: job.targetAccountId,
    baseLedgerVersion: job.previewLedgerVersion,
    totalRows: job.totalRows,
    insertedRows: job.insertedRows,
    skippedRows: job.skippedRows,
    errorCode: job.errorCode,
    committedLedgerVersion: job.committedLedgerVersion,
    createdAt: job.createdAt,
    updatedAt: job.updatedAt,
  };
}

/** 判断仓储值是否属于导入作业可公开错误码闭集。 */
export function isImportJobErrorCode(value: string): value is ImportJobErrorCode {
  return (IMPORT_JOB_ERROR_CODES as readonly string[]).includes(value);
}
