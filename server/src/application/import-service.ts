import { readFile } from "node:fs/promises";
import type { Readable } from "node:stream";
import { randomUUID } from "node:crypto";

import type { ImportJobStatus } from "./import-job.js";
import { transitionImportJob } from "./import-job.js";
import type { ImportPreviewResult } from "./import-preview.js";
import type { PublicImportPreview } from "./import-preview-view.js";
import { createPublicImportPreview } from "./import-preview-view.js";
import { createPublicImportJob } from "./import-job-view.js";
import {
  assertImportPreviewAuthorized,
  issueImportPreviewAuthorization,
} from "./import-preview-auth.js";
import { createImportPreview } from "./import-preview.js";
import { buildImportCommitPlan } from "./import-commit.js";
import type { ImportCommitPlan } from "./import-commit.js";
import { ApplicationError } from "./app-error.js";
import type { WriteCoordinator } from "./write-coordinator.js";
import { parseSchwabCsv } from "../infrastructure/imports/schwab-csv.js";
import { parseSchwabXlsx } from "../infrastructure/imports/schwab-xlsx.js";
import { stageImportUpload } from "../infrastructure/imports/staged-upload.js";
import type { StagedImportUpload } from "../infrastructure/imports/staged-upload.js";
import type { TransactionBatchInput } from "../domain/ledger/index.js";
import type { PersistImportCommitInput } from "../infrastructure/db/import-commit-repository.js";
import { persistImportCommit } from "../infrastructure/db/import-commit-repository.js";
import {
  createIdempotentRequestHash,
  hashIdempotencyKey,
  planIdempotentMutation,
} from "./idempotent-mutation.js";
import {
  loadMutationIdempotencyRecord,
  releaseMutationIdempotencyRecord,
  startMutationIdempotencyRecord,
} from "../infrastructure/db/mutation-idempotency-repository.js";
import {
  casImportJobStatus,
  createImportJobRecord,
  loadImportAccounts,
  loadImportJobRecord,
  loadImportPreviewAuthorization,
  saveImportPreview,
} from "../infrastructure/db/import-job-repository.js";
import { loadLedgerAnalysisSnapshot } from "../infrastructure/db/ledger-analysis-repository.js";
import { readCurrentLedgerVersion } from "../infrastructure/db/agent-session-repository.js";
import { resolveImportAccount } from "./import-account.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";

/** 解析规则版本；任何标准化规则变化都必须提升该版本使旧预览失效。 */
export const IMPORT_PARSER_VERSION = "1";

/** 预览确认令牌有效期；过期后必须重新预览。 */
const PREVIEW_AUTHORIZATION_TTL_MILLISECONDS = 10 * 60 * 1000;

/** 上传字节上限；与既有上传安全检查保持一致。 */
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

interface ActiveImportEntry {
  staged: StagedImportUpload;
  preview: ImportPreviewResult;
  accountId: string;
  currentStatus: ImportJobStatus;
}

/** 一次导入预览的 HTTP 响应载荷。 */
export interface ImportPreviewHttpResponse {
  importId: string;
  confirmToken: string;
  preview: PublicImportPreview;
}

/** 一次导入提交的 HTTP 响应载荷。 */
export interface ImportCommitHttpResponse {
  changed: boolean;
  ledgerVersion: number;
  insertedRows: number;
  skippedRows: number;
}

/**
 * 导入上传、预览、提交与取消的单进程编排。
 *
 * 解析后的标准交易只保存在进程内，提交由 commit 仓储与账本版本绑定原子落
 * 库；作业状态、预览指纹与一次性确认令牌哈希持久化在 DuckDB。进程重启会
 * 丢弃未提交的解析结果，App 必须重新上传预览。
 */
export class ImportService {
  readonly #database: LyraDatabase;
  readonly #writeCoordinator: WriteCoordinator;
  readonly #stagingDirectory: string;
  readonly #now: () => Date;
  readonly #entries = new Map<string, ActiveImportEntry>();

  constructor(input: {
    database: LyraDatabase;
    writeCoordinator: WriteCoordinator;
    stagingDirectory: string;
    now?: () => Date;
  }) {
    this.#database = input.database;
    this.#writeCoordinator = input.writeCoordinator;
    this.#stagingDirectory = input.stagingDirectory;
    this.#now = input.now ?? (() => new Date());
  }

  /**
   * 接收上传流并完成"暂存 → 解析 → 账户映射 → 预览 → 授权令牌"全流程。
   *
   * 作业先以 uploaded 创建、随后 CAS 到 parsing；映射或校验失败都会把作业
   * 收口到 rejected 并清理临时文件后原样抛出，只有 preview_ready 成功落库后
   * 才保留进程内解析结果。
   */
  async createPreviewFromUpload(input: {
    stream: Readable;
    fileName: string;
    explicitAccountId: string | null;
  }): Promise<ImportPreviewHttpResponse> {
    const importId = randomUUID();
    const staged = await stageImportUpload({
      stream: input.stream,
      stagingDirectory: this.#stagingDirectory,
      fileName: input.fileName,
      maxSizeBytes: MAX_UPLOAD_BYTES,
    });
    let currentStatus: ImportJobStatus = "uploaded";
    try {
      const summary = staged.summary;
      await createImportJobRecord(this.#database, {
        importId,
        fileHash: summary.sha256,
        fileName: input.fileName,
        fileType: summary.fileType,
        fileSizeBytes: summary.sizeBytes,
        parserVersion: IMPORT_PARSER_VERSION,
        createdAt: this.#now().toISOString(),
      });
      await casImportJobStatus(this.#database, {
        importId,
        from: "uploaded",
        to: "parsing",
        at: this.#now().toISOString(),
      });
      currentStatus = "parsing";

      const batch = await this.#parseBatch(staged);
      const resolved = resolveImportAccount(
        batch.account,
        await loadImportAccounts(this.#database),
        input.explicitAccountId,
      );
      const preview = await this.#buildPreview(batch, resolved.legacyAccountKey);
      const authorization = issueImportPreviewAuthorization(
        importId,
        preview.previewFingerprint,
        this.#now(),
        PREVIEW_AUTHORIZATION_TTL_MILLISECONDS,
      );
      await saveImportPreview(this.#database, {
        importId,
        accountId: resolved.accountId,
        fingerprint: preview.previewFingerprint,
        expiresAt: authorization.record.expiresAt,
        tokenHash: authorization.record.tokenHash,
        // 版本 0 在 ledger_versions 中没有对应行；提交仓储按 NULL 解释空账本。
        previewLedgerVersion: preview.baseLedgerVersion === 0 ? null : preview.baseLedgerVersion,
        totalRows: preview.totalRows,
        insertedRows: preview.insertableRows,
        skippedRows: preview.existingRows,
        at: this.#now().toISOString(),
      });
      this.#entries.set(importId, {
        staged,
        preview,
        accountId: resolved.accountId,
        currentStatus: "preview_ready",
      });
      return {
        importId,
        confirmToken: authorization.token,
        preview: createPublicImportPreview(preview, resolved.accountId),
      };
    } catch (error: unknown) {
      await this.#markFailed(importId, currentStatus, error);
      await staged.dispose();
      throw error;
    }
  }

  /** 读取导入作业的公开状态投影；不存在返回 null。 */
  async getImportJob(
    importId: string,
  ): Promise<ReturnType<typeof createPublicImportJob> | null> {
    const record = await loadImportJobRecord(this.#database, importId);
    if (record === null) {
      return null;
    }
    return createPublicImportJob(record);
  }

  /**
   * 在写队列内以最新账户事实生成提交计划并原子落库。
   *
   * 令牌、指纹与作业状态在进入写队列前校验；账本版本冲突由 commit 仓储在
   * 事务内再次把关，任何失败都不会写入部分账本。
   */
  async commitImport(input: {
    importId: string;
    confirmToken: string;
    /** 设备作用域幂等上下文；缺省时保持既有非幂等提交行为。 */
    idempotency?: { deviceId: string; key: string } | null;
  }): Promise<ImportCommitHttpResponse> {
    // 幂等判定必须先于进程内 entry 检查：成功提交后解析结果已被丢弃，
    // 客户端网络超时重试时只能依靠持久化的幂等记录重建安全响应。
    const operation = "import.commit";
    const requestHash = createIdempotentRequestHash(operation, {
      importId: input.importId,
      // 确认令牌只以单向散列参与指纹，明文不进入任何持久化记录。
      confirmToken: input.confirmToken,
    });
    const idempotencyPlan = input.idempotency === undefined || input.idempotency === null
      ? null
      : planIdempotentMutation({
        recordId: randomUUID(),
        deviceId: input.idempotency.deviceId,
        operation,
        key: input.idempotency.key,
        requestHash,
        existingRecord: await loadMutationIdempotencyRecord(this.#database, {
          deviceId: input.idempotency.deviceId,
          operation,
          keyHash: hashIdempotencyKey(input.idempotency.key),
        }),
        now: this.#now(),
      });
    if (idempotencyPlan?.kind === "replay") {
      const replayRecord = await loadImportJobRecord(
        this.#database,
        idempotencyPlan.resultResourceId,
      );
      if (replayRecord === null || replayRecord.status !== "committed") {
        throw new ApplicationError({
          code: "IDEMPOTENCY_KEY_REUSED",
          message: "幂等键已用于其他请求，请生成新的幂等键",
          statusCode: 409,
          retryable: false,
        });
      }
      const job = createPublicImportJob(replayRecord);
      return {
        changed: job.insertedRows !== null && job.insertedRows > 0,
        ledgerVersion: job.committedLedgerVersion ?? job.baseLedgerVersion ?? 0,
        insertedRows: job.insertedRows ?? 0,
        skippedRows: job.skippedRows ?? 0,
      };
    }

    const entry = this.#entries.get(input.importId);
    if (entry === undefined) {
      throw new ApplicationError({
        code: "IMPORT_PREVIEW_STALE",
        message: "导入预览不存在或已失效，必须重新预览",
        statusCode: 409,
        retryable: false,
      });
    }
    const authorization = await loadImportPreviewAuthorization(
      this.#database,
      input.importId,
    );
    const jobRecord = await loadImportJobRecord(this.#database, input.importId);
    if (
      authorization === null
      || jobRecord === null
      || authorization.tokenHash === null
      || authorization.expiresAt === null
    ) {
      throw new ApplicationError({
        code: "IMPORT_PREVIEW_STALE",
        message: "导入预览授权不存在，必须重新预览",
        statusCode: 409,
        retryable: false,
      });
    }
    assertImportPreviewAuthorized({
      record: {
        importId: authorization.importId,
        previewFingerprint: authorization.previewFingerprint,
        tokenHash: authorization.tokenHash,
        expiresAt: authorization.expiresAt,
      },
      importId: input.importId,
      previewFingerprint: entry.preview.previewFingerprint,
      token: input.confirmToken,
      jobStatus: jobRecord.status,
      now: this.#now(),
    });

    try {
      if (idempotencyPlan?.kind === "start") {
        // in_progress 记录先于写队列落库：并发同键请求在计划阶段就能读到
        // IDEMPOTENCY_REQUEST_IN_PROGRESS，不会重复进入提交事务。
        await startMutationIdempotencyRecord(this.#database, {
          record: idempotencyPlan.record,
          replacesExpiredRecordId: idempotencyPlan.replacesExpiredRecordId,
        });
      }
      const committed = await this.#writeCoordinator.enqueue(async () => {
        const snapshot = await loadLedgerAnalysisSnapshot(
          this.#database,
          entry.preview.baseLedgerVersion,
        );
        const accountTransactions = snapshot.transactions.filter(
          (transaction) => transaction.account === entry.preview.account,
        );
        const plan: ImportCommitPlan = buildImportCommitPlan(
          entry.preview,
          accountTransactions,
        );
        const commitInput: PersistImportCommitInput = {
          plan,
          importId: input.importId,
          accountId: entry.accountId,
          parserVersion: entry.preview.parserVersion,
          previewFingerprint: entry.preview.previewFingerprint,
          expectedLedgerVersion: entry.preview.baseLedgerVersion,
          committedAt: this.#now(),
        };
        if (idempotencyPlan?.kind === "start") {
          commitInput.idempotencyCompletion = {
            recordId: idempotencyPlan.record.id,
            resultResourceId: input.importId,
            responseStatusCode: 200,
          };
        }
        return persistImportCommit(this.#database, commitInput);
      });
      await this.#discardEntry(input.importId);
      return {
        changed: committed.changed,
        ledgerVersion: committed.ledgerVersion,
        insertedRows: committed.insertedRows,
        skippedRows: committed.skippedRows,
      };
    } catch (error: unknown) {
      // 业务失败释放 in_progress 记录：同一逻辑重试可重新开始，已完成记录
      // 是重放依据，永远不会被释放路径触碰。
      if (idempotencyPlan?.kind === "start") {
        await releaseMutationIdempotencyRecord(
          this.#database,
          idempotencyPlan.record.id,
        );
      }
      throw error;
    }
  }

  /** 取消 uploaded/preview_ready 作业并清理进程内解析结果。 */
  async cancelImport(importId: string): Promise<void> {
    const entry = this.#entries.get(importId);
    if (entry === undefined) {
      const record = await loadImportJobRecord(this.#database, importId);
      if (record === null) {
        throw new ApplicationError({
          code: "IMPORT_STATE_CONFLICT",
          message: "导入作业不存在",
          statusCode: 404,
          retryable: false,
        });
      }
      transitionImportJob(
        { importId: record.id, status: record.status, updatedAt: record.updatedAt },
        "cancelled",
        this.#now(),
      );
      await casImportJobStatus(this.#database, {
        importId,
        from: record.status,
        to: "cancelled",
        at: this.#now().toISOString(),
      });
      return;
    }
    transitionImportJob(
      {
        importId,
        status: entry.currentStatus,
        updatedAt: this.#now().toISOString(),
      },
      "cancelled",
      this.#now(),
    );
    await casImportJobStatus(this.#database, {
      importId,
      from: entry.currentStatus,
      to: "cancelled",
      at: this.#now().toISOString(),
    });
    await this.#discardEntry(importId);
  }

  async #parseBatch(staged: StagedImportUpload): Promise<TransactionBatchInput> {
    const content = await readFile(staged.filePath);
    const source = {
      fileName: staged.summary.fileName,
      sourceHash: staged.summary.sha256,
      sourceType: "csv" as const,
      sourceReference: `import-file:${staged.summary.sha256}`,
    };
    if (staged.summary.fileType === "csv") {
      return parseSchwabCsv({ ...source, content });
    }
    return parseSchwabXlsx({ ...source, content });
  }

  async #buildPreview(
    batch: TransactionBatchInput,
    legacyAccountKey: string,
  ): Promise<ImportPreviewResult> {
    const ledgerVersion = await readCurrentLedgerVersion(this.#database);
    const snapshot = await loadLedgerAnalysisSnapshot(this.#database, ledgerVersion);
    const existingHashes = new Set(
      snapshot.transactions
        .filter((transaction) => transaction.account === legacyAccountKey)
        .map((transaction) => transaction.transactionHash),
    );
    return createImportPreview({
      batch,
      parserVersion: IMPORT_PARSER_VERSION,
      baseLedgerVersion: ledgerVersion,
      existingTransactionHashes: existingHashes,
    });
  }

  /** 把作业 CAS 到 rejected/failed；已终态时保持幂等不覆盖。 */
  async #markFailed(
    importId: string,
    currentStatus: ImportJobStatus,
    error: unknown,
  ): Promise<void> {
    if (currentStatus === "uploaded" || currentStatus === "parsing") {
      const target: ImportJobStatus = currentStatus === "parsing"
        ? "rejected"
        : "failed";
      transitionImportJob(
        { importId, status: currentStatus, updatedAt: this.#now().toISOString() },
        target,
        this.#now(),
      );
      await casImportJobStatus(this.#database, {
        importId,
        from: currentStatus,
        to: target,
        at: this.#now().toISOString(),
        errorCode: error instanceof ApplicationError
          ? error.code
          : "IMPORT_VALIDATION_FAILED",
        errorSummary: error instanceof ApplicationError ? error.message : null,
      });
    }
  }

  async #discardEntry(importId: string): Promise<void> {
    const entry = this.#entries.get(importId);
    if (entry === undefined) {
      return;
    }
    this.#entries.delete(importId);
    await entry.staged.dispose();
  }
}
