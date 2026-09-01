import type { Decimal } from "decimal.js";

import { IngestError } from "../ledger/index.js";
import type { ParsedTransaction } from "../ledger/index.js";
import type { EmailTrade } from "./econfirm.js";

/** 一组已完成账户归属的邮件成交，保留来源名用于对账审计。 */
export interface EmailTradeSource {
  /** Gmail message id、上传文件名或同步批次标识。 */
  source: string;
  /** 已由账户尾号解析得到的完整账户标识。 */
  account: string;
  /** 按邮件展示顺序排列的成交。 */
  trades: readonly EmailTrade[];
}

/** 单笔邮件成交与标准账本的只读比较结果。 */
export interface EmailReconciliationRow {
  status: "matched" | "missing" | "conflict";
  source: string;
  account: string;
  transactionDate: string;
  action: EmailTrade["action"];
  symbol: string;
  quantity: Decimal;
  price: Decimal;
  fees: Decimal;
  amount: Decimal;
  /** 精确匹配时指向被消费的账本交易；其余状态为空。 */
  transactionHash: string | null;
  /** 冲突时为同账户、日期和代码的候选数；缺失为 0。 */
  candidates: number;
}

/** 一次只读邮件对账的汇总结果。 */
export interface EmailReconciliationResult {
  rows: readonly EmailReconciliationRow[];
  count: number;
  matched: number;
  missing: number;
  conflict: number;
  readOnly: true;
}

/**
 * 把 eConfirm 账户尾号解析为完整账户标识。
 *
 * 用户显式指定账户时只校验尾号；未指定时要求现有账户中恰好有一个匹配项。
 * 该函数不猜测首次导入账户，也不在多账户歧义时自动选择。
 *
 * @param suffix - 已由 eConfirm 提取的账户尾号。
 * @param knownAccounts - 当前账本中已有的完整账户标识。
 * @param explicitAccount - 用户明确选择的完整账户；未提供时传 `null`。
 * @returns 唯一且经过尾号校验的完整账户标识。
 * @throws IngestError 当显式账户不匹配、无现有账户或存在多个候选时抛出。
 */
export function resolveEmailAccount(
  suffix: string,
  knownAccounts: readonly string[],
  explicitAccount: string | null,
): string {
  if (explicitAccount !== null) {
    if (!explicitAccount.endsWith(suffix)) {
      throw new IngestError(
        `账户 ${JSON.stringify(explicitAccount)} 与邮件账户尾号 ${JSON.stringify(suffix)} 不匹配`,
      );
    }
    return explicitAccount;
  }

  const matches = [...new Set(knownAccounts)]
    .filter((account) => account.endsWith(suffix))
    .toSorted();
  if (matches.length === 1) {
    const [account] = matches;
    if (account !== undefined) {
      return account;
    }
  }
  if (matches.length === 0) {
    throw new IngestError(
      `账本中没有尾号为 ${JSON.stringify(suffix)} 的账户；首次邮件导入必须明确选择账户`,
    );
  }
  throw new IngestError(
    `账本中有多个尾号为 ${JSON.stringify(suffix)} 的账户；必须明确选择账户`,
  );
}

/**
 * 逐笔比较 eConfirm 成交与标准账本，且不修改任一输入。
 *
 * 精确匹配沿用 Schwab 迁移基线的八项业务键。相同成交按交易哈希顺序逐笔
 * 消费，避免一条账本记录同时匹配多封邮件；精确记录耗尽后，如同账户、日期、
 * 代码仍有记录则报告冲突，否则报告缺失。
 *
 * @param sources - 已完成完整账户归属的邮件成交来源。
 * @param transactions - 当前标准交易账本快照。
 * @returns 包含逐笔状态和汇总计数的只读结果。
 */
export function reconcileEmailTrades(
  sources: readonly EmailTradeSource[],
  transactions: readonly ParsedTransaction[],
): EmailReconciliationResult {
  const rows: EmailReconciliationRow[] = [];
  const consumed = new Map<string, number>();

  for (const source of sources) {
    for (const trade of source.trades) {
      const exact = transactions
        .filter((transaction) => exactMatch(transaction, source.account, trade))
        .toSorted((left, right) => left.transactionHash.localeCompare(right.transactionHash));
      const key = exactKey(source.account, trade);
      const occurrence = consumed.get(key) ?? 0;
      const matchedTransaction = exact[occurrence];

      if (matchedTransaction !== undefined) {
        consumed.set(key, occurrence + 1);
        rows.push(toRow(source, trade, "matched", matchedTransaction.transactionHash, 1));
        continue;
      }

      const candidates = transactions.filter((transaction) => (
        transaction.account === source.account
        && transaction.transactionDate === trade.tradeDate
        && transaction.rawSymbol === trade.symbol
      )).length;
      rows.push(toRow(
        source,
        trade,
        candidates > 0 ? "conflict" : "missing",
        null,
        candidates,
      ));
    }
  }

  return {
    rows,
    count: rows.length,
    matched: rows.filter((row) => row.status === "matched").length,
    missing: rows.filter((row) => row.status === "missing").length,
    conflict: rows.filter((row) => row.status === "conflict").length,
    readOnly: true,
  };
}

/** 判断标准账本交易是否与邮件成交的业务字段完全一致。 */
function exactMatch(
  transaction: ParsedTransaction,
  account: string,
  trade: EmailTrade,
): boolean {
  return transaction.account === account
    && transaction.transactionDate === trade.tradeDate
    && transaction.action === trade.action
    && transaction.rawSymbol === trade.symbol
    && decimalEquals(transaction.quantity, trade.quantity)
    && decimalEquals(transaction.price, trade.price)
    && (transaction.fees?.equals(trade.fees) ?? trade.fees.isZero())
    && decimalEquals(transaction.amount, trade.amount);
}

/** 比较必填交易金额字段；空值永远不与邮件成交相等。 */
function decimalEquals(value: Decimal | null, expected: Decimal): boolean {
  return value !== null && value.equals(expected);
}

/** 构造重复成交消费计数使用的稳定复合键。 */
function exactKey(account: string, trade: EmailTrade): string {
  return JSON.stringify([
    account,
    trade.tradeDate,
    trade.action,
    trade.symbol,
    trade.quantity.toString(),
    trade.price.toString(),
    trade.fees.toString(),
    trade.amount.toString(),
  ]);
}

/** 把一次比较结果转换为不含账本内部对象引用的输出行。 */
function toRow(
  source: EmailTradeSource,
  trade: EmailTrade,
  status: EmailReconciliationRow["status"],
  transactionHash: string | null,
  candidates: number,
): EmailReconciliationRow {
  return {
    status,
    source: source.source,
    account: source.account,
    transactionDate: trade.tradeDate,
    action: trade.action,
    symbol: trade.symbol,
    quantity: trade.quantity,
    price: trade.price,
    fees: trade.fees,
    amount: trade.amount,
    transactionHash,
    candidates,
  };
}
