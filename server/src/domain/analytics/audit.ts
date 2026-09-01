import type { FifoReplayResult } from "../fifo/index.js";
import type { ParsedTransaction } from "../ledger/index.js";
import { isAccountInScope } from "./scope.js";
import type {
  AccountScope,
  LedgerAuditCheck,
  LedgerAuditResult,
} from "./types.js";

/**
 * 只读审计原始账本与 FIFO 派生结果的一致性。
 *
 * 审计不重放或修复数据，只报告缺失字段、未反映交易、孤儿引用和非法批次数量。
 * 按既有 Python 行为，本功能不把已过期期权作为账本一致性错误。
 *
 * @param transactions - 当前标准交易账本。
 * @param fifo - 当前已持久化或待持久化的 FIFO 派生结果。
 * @param accountScope - 显式账户范围。
 * @returns 包含每项检查和总状态的只读报告。
 */
export function auditLedger(
  transactions: readonly ParsedTransaction[],
  fifo: Pick<FifoReplayResult, "openLots" | "realizedRecords">,
  accountScope: AccountScope,
): LedgerAuditResult {
  const scopedTransactions = transactions.filter((transaction) => (
    isAccountInScope(transaction.account, accountScope)
  ));
  const scopedLots = fifo.openLots.filter((lot) => isAccountInScope(lot.account, accountScope));
  const scopedRealized = fifo.realizedRecords.filter((record) => (
    isAccountInScope(record.account, accountScope)
  ));
  const transactionHashes = new Set(scopedTransactions.map((transaction) => transaction.transactionHash));
  const reflectedHashes = new Set<string>();
  for (const lot of scopedLots) {
    reflectedHashes.add(lot.openTransaction);
  }
  for (const record of scopedRealized) {
    reflectedHashes.add(record.openTransaction);
    reflectedHashes.add(record.closeTransaction);
  }

  const positionTransactions = scopedTransactions.filter((transaction) => (
    transaction.assetType === "stock" || transaction.assetType === "option"
  ));
  const invalidTradeFields = positionTransactions.filter((transaction) => (
    transaction.rawSymbol === null
    || transaction.underlying === null
    || transaction.quantity === null
    || transaction.quantity.lessThanOrEqualTo(0)
  )).length;
  const unreflectedTransactions = positionTransactions.filter((transaction) => (
    !reflectedHashes.has(transaction.transactionHash)
  )).length;
  const orphanOpenLots = scopedLots.filter((lot) => !transactionHashes.has(lot.openTransaction)).length;
  const orphanRealizedRows = scopedRealized.filter((record) => (
    !transactionHashes.has(record.openTransaction)
    || !transactionHashes.has(record.closeTransaction)
  )).length;
  const invalidLotQuantities = scopedLots.filter((lot) => (
    lot.openQuantity.lessThanOrEqualTo(0)
    || lot.remainingQuantity.lessThanOrEqualTo(0)
    || lot.remainingQuantity.greaterThan(lot.openQuantity)
  )).length;
  const derivationMissing = positionTransactions.length > 0
    && scopedLots.length === 0
    && scopedRealized.length === 0
    ? 1
    : 0;

  const rows: LedgerAuditCheck[] = [
    presenceCheck("transactions_present", scopedTransactions.length, "至少一条原始交易"),
    zeroCheck("invalid_trade_fields", invalidTradeFields),
    zeroCheck("unreflected_fifo_transactions", unreflectedTransactions),
    zeroCheck("orphan_open_lots", orphanOpenLots),
    zeroCheck("orphan_realized_rows", orphanRealizedRows),
    zeroCheck("invalid_lot_quantities", invalidLotQuantities),
    zeroCheck("fifo_derivatives_present", derivationMissing),
  ];
  const failed = rows.filter((row) => row.status === "fail").length;
  const dates = scopedTransactions.map((transaction) => transaction.transactionDate).toSorted();

  return {
    rows,
    ok: failed === 0,
    failed,
    transactionCount: scopedTransactions.length,
    latestTransactionDate: dates.at(-1) ?? null,
    readOnly: true,
    scopeNote: "按用户要求，本功能不检查已过期期权",
  };
}

/** 构造要求至少存在一条记录的检查。 */
function presenceCheck(check: string, count: number, expectation: string): LedgerAuditCheck {
  return {
    check,
    severity: "error",
    count,
    expectation,
    status: count > 0 ? "pass" : "fail",
  };
}

/** 构造要求异常计数为零的检查。 */
function zeroCheck(check: string, count: number): LedgerAuditCheck {
  return {
    check,
    severity: "error",
    count,
    expectation: "0",
    status: count === 0 ? "pass" : "fail",
  };
}
