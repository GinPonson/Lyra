import {
  createOpaqueCredential,
  verifyOpaqueCredential,
} from "../infrastructure/security/index.js";
import type { ImportJobStatus } from "./import-job.js";
import { ApplicationError } from "./app-error.js";

/** 预览确认令牌对应的服务端持久化记录，不包含明文令牌。 */
export interface ImportPreviewAuthorizationRecord {
  importId: string;
  previewFingerprint: string;
  tokenHash: string;
  expiresAt: string;
}

/** 创建预览确认令牌时唯一一次返回给 App 的明文与持久化记录。 */
export interface IssuedImportPreviewAuthorization {
  token: string;
  record: ImportPreviewAuthorizationRecord;
}

/**
 * 为已经完整验证的导入预览签发短时不透明确认令牌。
 *
 * @param importId - 当前导入作业 ID。
 * @param previewFingerprint - 绑定文件、解析器、账户、账本版本和交易哈希的指纹。
 * @param now - 服务端可信时钟。
 * @param ttlMilliseconds - 预览可确认的有效期毫秒数。
 */
export function issueImportPreviewAuthorization(
  importId: string,
  previewFingerprint: string,
  now: Date,
  ttlMilliseconds: number,
): IssuedImportPreviewAuthorization {
  if (importId.trim().length === 0 || !/^[a-f0-9]{64}$/.test(previewFingerprint)) {
    throw new RangeError("导入 ID 或预览指纹无效");
  }
  if (!Number.isSafeInteger(ttlMilliseconds) || ttlMilliseconds <= 0) {
    throw new RangeError("导入预览有效期必须是正安全整数毫秒数");
  }
  if (Number.isNaN(now.getTime())) {
    throw new RangeError("导入预览签发时间无效");
  }
  const credential = createOpaqueCredential();
  return {
    token: credential.token,
    record: {
      importId,
      previewFingerprint,
      tokenHash: credential.tokenHash,
      expiresAt: new Date(now.getTime() + ttlMilliseconds).toISOString(),
    },
  };
}

/**
 * 在提交事务开始前验证作业状态、预览指纹、有效期和不透明令牌。
 *
 * 校验通过后，调用方必须以 compare-and-swap 将作业从 preview_ready 更新为
 * committing；该状态转换负责一次性消费语义，不能只调用本函数后直接写账本。
 */
export function assertImportPreviewAuthorized(input: {
  record: ImportPreviewAuthorizationRecord;
  importId: string;
  previewFingerprint: string;
  token: string;
  jobStatus: ImportJobStatus;
  now: Date;
}): void {
  const changedFields: string[] = [];
  if (input.record.importId !== input.importId) {
    changedFields.push("import_id");
  }
  if (input.record.previewFingerprint !== input.previewFingerprint) {
    changedFields.push("preview_fingerprint");
  }
  if (input.jobStatus !== "preview_ready") {
    changedFields.push("job_status");
  }
  if (Number.isNaN(input.now.getTime()) || input.now.getTime() >= Date.parse(input.record.expiresAt)) {
    changedFields.push("expires_at");
  }
  if (!verifyOpaqueCredential(input.token, input.record.tokenHash)) {
    changedFields.push("preview_token");
  }
  if (changedFields.length > 0) {
    throw new ApplicationError({
      code: "IMPORT_PREVIEW_STALE",
      message: "导入预览授权已失效，必须重新预览",
      statusCode: 409,
      retryable: false,
      details: { changed_fields: changedFields },
    });
  }
}
