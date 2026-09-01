/** 只读分析领域模块公共导出。 */
export { auditLedger } from "./audit.js";
export { formatIsoDate, parseIsoDate, validateDateRange } from "./dates.js";
export { aggregatePositions, analyzeExpiringPositions } from "./positions.js";
export {
  calculateAllocation,
  calculateAttribution,
  calculateMonthlyPerformance,
} from "./performance.js";
export { calculateDrawdown, calculateOptionRisk } from "./risk.js";
export { queryCashflow, queryRealized, queryTrades } from "./queries.js";
export type {
  CashflowQueryFilters,
  RealizedQueryFilters,
  TradeQueryFilters,
} from "./queries.js";
export {
  aggregateClosedTransactions,
  calculateExtremeTrades,
  calculateHoldingPeriod,
  calculateOptionStress,
  calculateStreaks,
} from "./trade-analysis.js";
export { calculateAccountSummary } from "./summary.js";
export type {
  AccountScope,
  AccountSummary,
  AllocationGroupBy,
  AllocationResult,
  AllocationRow,
  AttributionGroupBy,
  AttributionResult,
  AttributionRow,
  CashflowQueryResult,
  DrawdownResult,
  DrawdownRow,
  ClosedTransaction,
  ExtremeTradeRow,
  ExtremeTradesResult,
  ExpiringPositionRow,
  ExpiringPositionsResult,
  MonthlyPerformanceResult,
  MonthlyPerformanceRow,
  LongOptionUnderlyingRisk,
  HoldingPeriodResult,
  HoldingPeriodRow,
  LedgerAuditCheck,
  LedgerAuditResult,
  OptionRiskBucket,
  OptionRiskResult,
  OptionRiskRow,
  OptionStressResult,
  OptionStressRow,
  StreakRow,
  StreaksResult,
  PositionRow,
  RealizedQueryResult,
  TradeQueryResult,
} from "./types.js";
