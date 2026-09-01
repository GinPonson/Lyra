import { parseTransactionRow, withOccurrence } from "./transactions.js";
import type {
  ParsedTransaction,
  TransactionBatchInput,
  TransactionBatchPreview,
} from "./types.js";

/**
 * 标准化一个 Schwab 文件内的全部交易行并生成预览核心数据。
 *
 * 函数只在所有行都成功后返回结果；任何一行抛错都会直接终止调用，不返回
 * 部分交易。完全相同的业务交易按文件出现顺序编号，使两个真实重复交易获得
 * 不同 `transactionHash`，同时保持跨快照导入时可比较。
 *
 * @param input - 文件名、账户、来源哈希和已完成 CSV/Excel 语法解析的行。
 * @returns 全部标准交易、动作统计和文件元数据。
 * @throws IngestError 透传具体行的领域校验错误。
 */
export function normalizeTransactionBatch(input: TransactionBatchInput): TransactionBatchPreview {
  const occurrenceByBusinessHash = new Map<string, number>();
  const byAction: Record<string, number> = {};
  const transactions: ParsedTransaction[] = [];

  for (const [index, fields] of input.rows.entries()) {
    const sequence = index + 1;
    const parsed = parseTransactionRow(fields, {
      account: input.account,
      sourceHash: input.sourceHash,
      sourceType: input.sourceType,
      sourceReference: input.sourceReference,
      sequence,
      where: `${input.fileName} 第${sequence}行`,
    });

    const occurrence = (occurrenceByBusinessHash.get(parsed.businessHash) ?? 0) + 1;
    occurrenceByBusinessHash.set(parsed.businessHash, occurrence);
    const transaction = withOccurrence(parsed, occurrence);

    transactions.push(transaction);
    byAction[transaction.action] = (byAction[transaction.action] ?? 0) + 1;
  }

  return {
    fileName: input.fileName,
    account: input.account,
    sourceHash: input.sourceHash,
    totalRows: transactions.length,
    byAction,
    transactions,
  };
}
