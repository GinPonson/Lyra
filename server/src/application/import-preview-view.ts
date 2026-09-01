import type { ImportPreviewResult } from "./import-preview.js";

/** App 确认导入前可见的最小预览，不包含可写入账本的内部交易。 */
export interface PublicImportPreview {
  fileName: string;
  accountId: string;
  baseLedgerVersion: number;
  totalRows: number;
  insertableRows: number;
  existingRows: number;
  byAction: Readonly<Record<string, number>>;
  firstTransactionDate: string | null;
  lastTransactionDate: string | null;
}

/**
 * 将完整导入预览投影为 App 可复核的统计视图。
 *
 * `transactions`、迁移期旧账户键、文件哈希、解析器版本和预览指纹只用于服务端
 * 提交校验，不进入客户端响应。客户端通过独立的 importId 与短时确认令牌提交。
 *
 * @param preview - 已完成全批标准化的内部预览。
 * @param targetAccountId - 用户已明确选择的 Lyra 账户 UUID。
 */
export function createPublicImportPreview(
  preview: ImportPreviewResult,
  targetAccountId: string,
): PublicImportPreview {
  const accountId = targetAccountId.trim();
  if (accountId.length === 0) {
    throw new RangeError("导入预览目标账户 ID 不能为空");
  }
  return {
    fileName: preview.fileName,
    accountId,
    baseLedgerVersion: preview.baseLedgerVersion,
    totalRows: preview.totalRows,
    insertableRows: preview.insertableRows,
    existingRows: preview.existingRows,
    byAction: { ...preview.byAction },
    firstTransactionDate: preview.firstTransactionDate,
    lastTransactionDate: preview.lastTransactionDate,
  };
}
