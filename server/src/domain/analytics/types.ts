import type { Decimal } from "decimal.js";

import type { LocalDate } from "../ledger/index.js";

/** 所有分析调用必须显式指定单账户或全部账户，禁止缺省聚合。 */
export type AccountScope =
  | { kind: "account"; account: string }
  | { kind: "all" };

/** 与 Python `v_positions` 相同口径的当前持仓聚合行。 */
export interface PositionRow {
  account: string;
  symbolKey: string | null;
  assetType: "stock" | "option";
  underlying: string | null;
  expiry: LocalDate | null;
  strike: Decimal | null;
  optionType: "C" | "P" | null;
  direction: "long" | "short";
  quantity: Decimal;
  /** 历史开仓价格乘剩余数量，不含费用；期权尚未乘 100。 */
  cost: Decimal;
  firstOpenDate: LocalDate;
}

/** 到期窗口内的期权持仓及相对基准日状态。 */
export interface ExpiringPositionRow extends PositionRow {
  status: "expired" | "expires_today" | "upcoming";
  daysRemaining: number;
  /** 历史权利金美元金额，等于 `cost × 100`。 */
  premiumDollars: Decimal;
}

/** 到期期权分析返回值。 */
export interface ExpiringPositionsResult {
  rows: readonly ExpiringPositionRow[];
  count: number;
  asOf: LocalDate;
  cutoff: LocalDate;
  expired: number;
  expiresToday: number;
  upcoming: number;
}

/** 账户总览的稳定 API 口径；所有精确数值均序列化为字符串。 */
export interface AccountSummary {
  accountScope: AccountScope;
  netDeposits: string;
  realizedPnlStock: string;
  realizedPnlOption: string;
  realizedPnlTotal: string;
  openPositions: number;
  totalFees: string;
  dividends: string;
  interest: string;
  taxes: string;
  cashBalance: string;
  transactionCount: number;
  dateRange: string | null;
}

/** 单个月份的 FIFO 已实现绩效。 */
export interface MonthlyPerformanceRow {
  month: string;
  closedLots: number;
  closeTransactions: number;
  wins: number;
  losses: number;
  winRatePct: string | null;
  grossProfit: string;
  grossLoss: string;
  profitFactor: string | null;
  netPnl: string;
  fees: string;
}

/** 月度绩效结果。 */
export interface MonthlyPerformanceResult {
  rows: readonly MonthlyPerformanceRow[];
  months: number;
  totalPnl: string;
  totalFees: string;
  basis: "realized_fifo";
}

/** 当前仓位配置支持的聚合维度。 */
export type AllocationGroupBy = "underlying" | "asset-type";

/** 一条历史成本配置聚合。 */
export interface AllocationRow {
  category: string | null;
  positions: number;
  longPositions: number;
  shortPositions: number;
  costBasis: string;
  allocationPct: string | null;
}

/** 当前仓位历史成本配置结果。 */
export interface AllocationResult {
  rows: readonly AllocationRow[];
  groups: number;
  totalCostBasis: string;
  basis: "historical_cost_not_market_value";
}

/** 已实现损益归因支持的聚合维度。 */
export type AttributionGroupBy = "underlying" | "asset-type" | "direction" | "close-action";

/** 一条已实现损益归因聚合。 */
export interface AttributionRow {
  category: string | null;
  closedLots: number;
  closeTransactions: number;
  wins: number;
  losses: number;
  winRatePct: string | null;
  netPnl: string;
  absoluteContributionPct: string | null;
  fees: string;
}

/** 已实现损益归因结果。 */
export interface AttributionResult {
  rows: readonly AttributionRow[];
  groups: number;
  totalPnl: string;
  basis: "realized_fifo";
}

/** 单日累计已实现损益与回撤。 */
export interface DrawdownRow {
  date: LocalDate;
  dailyPnl: string;
  cumulativePnl: string;
  runningPeak: string;
  drawdown: string;
}

/** 基于日度 FIFO 已实现损益的回撤结果。 */
export interface DrawdownResult {
  rows: readonly DrawdownRow[];
  days: number;
  endingCumulativePnl: string;
  maxDrawdown: string;
  peakDate: LocalDate | null;
  troughDate: LocalDate | null;
  recoveryDate: LocalDate | null;
  basis: "daily_realized_fifo";
}

/** 期权到期风险桶。 */
export type OptionRiskBucket = "expired" | "0-30d" | "31-60d" | "61-90d" | "90d+";

/** 单个到期窗口的长期权成本与短期权收入。 */
export interface OptionRiskRow {
  bucket: OptionRiskBucket;
  positions: number;
  contracts: string;
  longPositions: number;
  shortPositions: number;
  longContracts: string;
  shortContracts: string;
  longPremiumPaid: string;
  shortPremiumReceived: string;
  longPremiumPct: string | null;
}

/** 长期权按标的的成本集中度。 */
export interface LongOptionUnderlyingRisk {
  underlying: string | null;
  longPremiumPaid: string;
  longPremiumPct: string | null;
}

/** 当前期权成本风险日历。 */
export interface OptionRiskResult {
  rows: readonly OptionRiskRow[];
  asOf: LocalDate;
  optionPositions: number;
  totalLongPremiumPaid: string;
  totalShortPremiumReceived: string;
  topThreeLongUnderlyings: readonly LongOptionUnderlyingRisk[];
  topThreeLongConcentrationPct: string | null;
  basis: "long_paid_and_short_received_premium_not_market_risk";
}

/** 聚合多个 FIFO lot 后的一笔平仓交易。 */
export interface ClosedTransaction {
  closeTransaction: string;
  account: string;
  closeDate: LocalDate;
  symbolKey: string | null;
  underlying: string | null;
  assetType: "stock" | "option";
  direction: "long" | "short";
  closeAction: string;
  quantity: Decimal;
  pnl: Decimal;
  fees: Decimal;
  earliestOpenDate: LocalDate;
  latestOpenDate: LocalDate;
}

/** FIFO lot 持有周期绩效。 */
export interface HoldingPeriodRow {
  bucket: "0-7d" | "8-30d" | "31-90d" | "90d+";
  closedLots: number;
  closeTransactions: number;
  wins: number;
  losses: number;
  winRatePct: string | null;
  averageHoldingDays: string;
  averageLotPnl: string;
  netPnl: string;
}

/** 持有周期分析结果。 */
export interface HoldingPeriodResult {
  rows: readonly HoldingPeriodRow[];
  buckets: number;
  basis: "realized_fifo_lots";
}

/** 最佳或最差平仓交易排行项。 */
export interface ExtremeTradeRow {
  side: "best" | "worst";
  rank: number;
  trade: ClosedTransaction;
}

/** 最佳与最差交易结果。 */
export interface ExtremeTradesResult {
  rows: readonly ExtremeTradeRow[];
  perSide: number;
  basis: "aggregated_close_transaction";
}

/** 最长连续盈利或亏损区间。 */
export interface StreakRow {
  kind: "win" | "loss";
  count: number;
  startDate: LocalDate;
  endDate: LocalDate;
  pnl: string;
}

/** 连续盈亏分析结果。 */
export interface StreaksResult {
  rows: readonly StreakRow[];
  closeTransactions: number;
  neutralTransactions: number;
  basis: "aggregated_close_transaction";
}

/** 单个长期权标的归零压力。 */
export interface OptionStressRow {
  underlying: string | null;
  contracts: string;
  premiumAtRisk: string;
  lossIfZero: string;
  portfolioPremiumPct: string | null;
  nearestExpiry: LocalDate | null;
  farthestExpiry: LocalDate | null;
}

/** 当前全部长期权权利金归零压力结果。 */
export interface OptionStressResult {
  rows: readonly OptionStressRow[];
  longOptionContracts: string;
  allLongOptionsLossIfZero: string;
  topThreeLossIfZero: string;
  basis: "maximum_paid_premium_loss_no_market_data";
}

/** 单项账本一致性检查。 */
export interface LedgerAuditCheck {
  check: string;
  severity: "error";
  count: number;
  expectation: string;
  status: "pass" | "fail";
}

/** 账本与 FIFO 派生结果的只读审计报告。 */
export interface LedgerAuditResult {
  rows: readonly LedgerAuditCheck[];
  ok: boolean;
  failed: number;
  transactionCount: number;
  latestTransactionDate: LocalDate | null;
  readOnly: true;
  scopeNote: string;
}

/** 基础交易流水查询结果。 */
export interface TradeQueryResult {
  rows: readonly import("../ledger/index.js").ParsedTransaction[];
  count: number;
  /** 还有后续数据时返回；末页为 null。 */
  nextCursor: string | null;
}

/** 已实现损益明细和筛选范围内合计。 */
export interface RealizedQueryResult {
  rows: readonly import("../fifo/index.js").RealizedRecord[];
  /** 当前页行数。 */
  count: number;
  /** 当前筛选范围的完整行数。 */
  totalCount: number;
  /** 当前筛选范围的完整合计，不随分页变化。 */
  totalPnl: string;
  nextCursor: string | null;
}

/** 现金流水明细和分类合计。 */
export interface CashflowQueryResult {
  rows: readonly import("../ledger/index.js").ParsedTransaction[];
  /** 当前页行数。 */
  count: number;
  /** 当前筛选范围的完整行数。 */
  totalCount: number;
  /** 当前筛选范围的完整合计，不随分页变化。 */
  total: string;
  byAction: Readonly<Record<string, string>>;
  nextCursor: string | null;
}
