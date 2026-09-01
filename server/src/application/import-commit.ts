import {
  replayFifo,
} from "../domain/fifo/index.js";
import type {
  FifoReplayResult,
} from "../domain/fifo/index.js";
import type {
  ParsedTransaction,
} from "../domain/ledger/index.js";
import type {
  ImportPreviewResult,
} from "./import-preview.js";

/** 数据库事务开始前已经完成计算的导入变更计划。 */
export interface ImportCommitPlan {
  account: string;
  changed: boolean;
  insertedRows: number;
  skippedRows: number;
  /** 仅包含本次需要插入标准事实表的交易。 */
  insertedTransactions: readonly ParsedTransaction[];
  /** 受影响账户提交后的完整交易快照。 */
  accountTransactions: readonly ParsedTransaction[];
  /** 有新增交易时的完整 FIFO 产物；无变更时不做无收益重放。 */
  fifo: FifoReplayResult | null;
}

/**
 * 根据当前账户事实表与已确认预览生成原子提交计划。
 *
 * 该函数在打开写事务前完成去重和 FIFO 重放。数据库适配器进入单写队列后仍需
 * 再次校验账本版本，然后在一个事务中插入交易并替换该账户的 lots/realized。
 *
 * @param preview - 已通过提交快照校验的导入预览。
 * @param existingAccountTransactions - 当前目标账户的完整标准交易快照。
 * @returns 可直接交给 DuckDB 适配器执行的确定性计划。
 * @throws Error 当仓储返回其他账户数据时抛出。
 * @throws FifoError 当新增交易会使 FIFO 账本无效时由领域层抛出。
 */
export function buildImportCommitPlan(
  preview: ImportPreviewResult,
  existingAccountTransactions: readonly ParsedTransaction[],
): ImportCommitPlan {
  const foreignAccount = existingAccountTransactions.find((transaction) => (
    transaction.account !== preview.account
  ));
  if (foreignAccount !== undefined) {
    throw new Error(
      `仓储返回了目标账户之外的交易: expected=${preview.account} actual=${foreignAccount.account}`,
    );
  }

  const existingHashes = new Set(
    existingAccountTransactions.map((transaction) => transaction.transactionHash),
  );
  const insertedTransactions = preview.transactions.filter((transaction) => (
    !existingHashes.has(transaction.transactionHash)
  ));
  if (insertedTransactions.length === 0) {
    return {
      account: preview.account,
      changed: false,
      insertedRows: 0,
      skippedRows: preview.transactions.length,
      insertedTransactions: [],
      accountTransactions: existingAccountTransactions,
      fifo: null,
    };
  }

  const accountTransactions = [
    ...existingAccountTransactions,
    ...insertedTransactions,
  ];
  const fifo = replayFifo(accountTransactions);
  return {
    account: preview.account,
    changed: true,
    insertedRows: insertedTransactions.length,
    skippedRows: preview.transactions.length - insertedTransactions.length,
    insertedTransactions,
    accountTransactions,
    fifo,
  };
}
