import { Decimal } from "decimal.js";

import type { FifoReplayResult } from "../fifo/index.js";
import type { ParsedTransaction } from "../ledger/index.js";
import { aggregatePositions } from "./positions.js";
import { isAccountInScope } from "./scope.js";
import type { AccountScope, AccountSummary } from "./types.js";

/** 会计上计入股息合计的 Schwab 动作。 */
const DIVIDEND_ACTIONS = new Set([
  "Qualified Dividend",
  "Non-Qualified Div",
  "Pr Yr Non-Qual Div",
]);

/** 会计上计入税项合计的 Schwab 动作。 */
const TAX_ACTIONS = new Set([
  "NRA Tax Adj",
  "Pr Yr NRA Tax",
  "Foreign Tax Paid",
]);

/**
 * 按显式账户范围计算 Python `summary` 的稳定总览口径。
 *
 * @param transactions - 标准交易账本。
 * @param fifo - 与该账本版本对应的 FIFO 重放结果。
 * @param accountScope - 单账户或全部账户，调用方不可省略。
 * @returns 精确金额已序列化为固定小数字符串的总览。
 */
export function calculateAccountSummary(
  transactions: readonly ParsedTransaction[],
  fifo: Pick<FifoReplayResult, "openLots" | "realizedRecords">,
  accountScope: AccountScope,
): AccountSummary {
  const scopedTransactions = transactions.filter((transaction) => (
    isAccountInScope(transaction.account, accountScope)
  ));
  const scopedRealized = fifo.realizedRecords.filter((record) => (
    isAccountInScope(record.account, accountScope)
  ));
  const scopedLots = fifo.openLots.filter((lot) => isAccountInScope(lot.account, accountScope));

  const realizedStock = sumDecimals(
    scopedRealized.filter((record) => record.assetType === "stock").map((record) => record.pnl),
  );
  const realizedOption = sumDecimals(
    scopedRealized.filter((record) => record.assetType === "option").map((record) => record.pnl),
  );
  const dates = scopedTransactions.map((transaction) => transaction.transactionDate).toSorted();

  return {
    accountScope,
    netDeposits: formatAmount(sumTransactionAmounts(
      scopedTransactions.filter((transaction) => transaction.action === "MoneyLink Transfer"),
    )),
    realizedPnlStock: formatAmount(realizedStock),
    realizedPnlOption: formatAmount(realizedOption),
    realizedPnlTotal: formatAmount(realizedStock.plus(realizedOption)),
    openPositions: aggregatePositions(scopedLots, accountScope).length,
    totalFees: sumDecimals(scopedTransactions.map((transaction) => transaction.fees)).toFixed(6),
    dividends: formatAmount(sumTransactionAmounts(
      scopedTransactions.filter((transaction) => DIVIDEND_ACTIONS.has(transaction.action)),
    )),
    interest: formatAmount(sumTransactionAmounts(
      scopedTransactions.filter((transaction) => transaction.action === "Credit Interest"),
    )),
    taxes: formatAmount(sumTransactionAmounts(
      scopedTransactions.filter((transaction) => TAX_ACTIONS.has(transaction.action)),
    )),
    cashBalance: formatAmount(sumTransactionAmounts(scopedTransactions)),
    transactionCount: scopedTransactions.length,
    dateRange: dates.length === 0 ? null : `${dates[0]} ~ ${dates.at(-1)}`,
  };
}

/** 合计交易 Amount；空值与 SQL `sum` 一样不贡献数值。 */
function sumTransactionAmounts(transactions: readonly ParsedTransaction[]): Decimal {
  return sumDecimals(transactions.map((transaction) => transaction.amount));
}

/** 合计可空 Decimal，并显式以 Decimal 零作为空集合起始值。 */
function sumDecimals(values: readonly (Decimal | null)[]): Decimal {
  return values.reduce<Decimal>(
    (total, value) => value === null ? total : total.plus(value),
    new Decimal(0),
  );
}

/** Amount 和 realized PnL 按 DuckDB 既有 schema 的四位小数输出。 */
function formatAmount(value: Decimal): string {
  return value.toDecimalPlaces(4, Decimal.ROUND_HALF_EVEN).toFixed(4);
}
