import { createHash } from "node:crypto";

import {
  normalizeTransactionBatch,
} from "../domain/ledger/index.js";
import type {
  ParsedTransaction,
  TransactionBatchInput,
} from "../domain/ledger/index.js";
import { ApplicationError } from "./app-error.js";

/** 创建导入预览需要的账本快照信息。 */
export interface CreateImportPreviewInput {
  batch: TransactionBatchInput;
  /** 标准化规则版本；规则变化后旧预览必须失效。 */
  parserVersion: string;
  /** 预览时观察到的最新账本版本；空账本使用 0。 */
  baseLedgerVersion: number;
  /** 当前目标账户中已经存在的规范交易哈希。 */
  existingTransactionHashes: ReadonlySet<string>;
}

/** 可安全返回给导入预览 API 的完整应用层结果。 */
export interface ImportPreviewResult {
  previewFingerprint: string;
  fileName: string;
  fileHash: string;
  parserVersion: string;
  account: string;
  baseLedgerVersion: number;
  totalRows: number;
  insertableRows: number;
  existingRows: number;
  byAction: Readonly<Record<string, number>>;
  firstTransactionDate: string | null;
  lastTransactionDate: string | null;
  /** 提交阶段复用的完整标准交易；HTTP 序列化层不直接暴露该字段。 */
  transactions: readonly ParsedTransaction[];
}

/** 提交时重新读取的不可变条件。 */
export interface ImportCommitSnapshot {
  fileHash: string;
  parserVersion: string;
  account: string;
  ledgerVersion: number;
}

/**
 * 全量校验批次并生成绑定当前账本快照的导入预览。
 *
 * @param input - 已由 CSV/Excel 适配器提取的八列数据及当前账本哈希集合。
 * @returns 包含预计新增/跳过数、日期范围和提交指纹的预览。
 * @throws IngestError 当任一交易行不符合标准化规则时由领域层抛出。
 * @throws RangeError 当账本版本或解析器版本无效时抛出。
 */
export function createImportPreview(input: CreateImportPreviewInput): ImportPreviewResult {
  if (!Number.isSafeInteger(input.baseLedgerVersion) || input.baseLedgerVersion < 0) {
    throw new RangeError("baseLedgerVersion 必须是非负安全整数");
  }
  if (input.parserVersion.trim().length === 0) {
    throw new RangeError("parserVersion 不能为空");
  }

  const normalized = normalizeTransactionBatch(input.batch);
  const existingRows = normalized.transactions.filter((transaction) => (
    input.existingTransactionHashes.has(transaction.transactionHash)
  )).length;
  const dates = normalized.transactions
    .map((transaction) => transaction.transactionDate)
    .toSorted();
  const fingerprint = createHash("sha256")
    .update(JSON.stringify({
      fileHash: input.batch.sourceHash,
      parserVersion: input.parserVersion,
      account: input.batch.account,
      baseLedgerVersion: input.baseLedgerVersion,
      transactionHashes: normalized.transactions.map((transaction) => transaction.transactionHash),
    }))
    .digest("hex");

  return {
    previewFingerprint: fingerprint,
    fileName: normalized.fileName,
    fileHash: normalized.sourceHash,
    parserVersion: input.parserVersion,
    account: normalized.account,
    baseLedgerVersion: input.baseLedgerVersion,
    totalRows: normalized.totalRows,
    insertableRows: normalized.totalRows - existingRows,
    existingRows,
    byAction: normalized.byAction,
    firstTransactionDate: dates[0] ?? null,
    lastTransactionDate: dates.at(-1) ?? null,
    transactions: normalized.transactions,
  };
}

/**
 * 在进入数据库事务前确认提交条件仍与预览完全一致。
 *
 * @param preview - 用户确认的既有预览。
 * @param current - 提交瞬间重新读取的文件、解析器、账户与账本版本。
 * @throws ApplicationError 当任一绑定条件变化时抛出，调用方必须要求重新预览。
 */
export function assertImportPreviewCurrent(
  preview: ImportPreviewResult,
  current: ImportCommitSnapshot,
): void {
  const changed: string[] = [];
  if (preview.fileHash !== current.fileHash) {
    changed.push("file_hash");
  }
  if (preview.parserVersion !== current.parserVersion) {
    changed.push("parser_version");
  }
  if (preview.account !== current.account) {
    changed.push("account");
  }
  if (preview.baseLedgerVersion !== current.ledgerVersion) {
    changed.push("ledger_version");
  }
  if (changed.length > 0) {
    throw new ApplicationError({
      code: "IMPORT_PREVIEW_STALE",
      message: "导入预览已失效，必须重新预览",
      statusCode: 409,
      retryable: false,
      details: { changed_fields: changed },
    });
  }
}
