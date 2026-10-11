import {
  FinancialToolRegistry,
  type AgentToolContext,
} from "../domain/agent/index.js";
import {
  aggregatePositions,
  analyzeExpiringPositions,
  auditLedger,
  calculateAllocation,
  calculateAccountSummary,
  calculateAttribution,
  calculateDrawdown,
  calculateExtremeTrades,
  calculateHoldingPeriod,
  calculateMonthlyPerformance,
  calculateOptionRisk,
  calculateOptionStress,
  calculateStreaks,
  queryCashflow,
  queryRealized,
  queryTrades,
  type AllocationGroupBy,
  type AccountSummary,
  type AttributionGroupBy,
  type CashflowQueryFilters,
  type ExtremeTradesResult,
  type RealizedQueryFilters,
  type TradeQueryFilters,
} from "../domain/analytics/index.js";
import type { Lot, RealizedRecord } from "../domain/fifo/index.js";
import type {
  LedgerSourceType,
  ParsedTransaction,
  TransactionAction,
} from "../domain/ledger/index.js";
import {
  loadLedgerAnalysisSnapshot,
  type LedgerAnalysisSnapshot,
} from "../infrastructure/db/ledger-analysis-repository.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";
import { resolveAccountScope, type ResolvedAccountScope } from "./account-scope.js";
import { createPublicAccountList } from "./account-view.js";
import { MarketDataService } from "./market-data-service.js";
import { createMarketPositionView } from "./market-position-view.js";
import type { MarketDataReader } from "../domain/market-data/types.js";
import {
  createPublicExpiringPositions,
  createPublicPositionList,
  createPublicCashflowQuery,
  createPublicRealizedQuery,
  createPublicTradeQuery,
} from "./ledger-detail-view.js";

/** 金额类 Agent 分析共用的单币种结果。 */
export interface AgentCurrencyGroup<T> {
  currency: string;
  accountIds: readonly string[];
  data: T;
}

/** 金额类 Agent 分析共用的分币种结果，禁止伪造跨币种总计。 */
export interface AgentCurrencyGroupedResult<T> {
  aggregateCurrency: string | null;
  groups: readonly AgentCurrencyGroup<T>[];
}

/** Agent `summary` 的稳定公开结果。 */
export type AgentAccountSummaryResult = AgentCurrencyGroupedResult<
  Omit<AccountSummary, "accountScope">
>;

/**
 * 创建连接真实 DuckDB 的金融注册表；行情单独注入，历史账本工具不访问行情源。
 *
 * 每次工具调用都使用凭据中的账本版本读取单条 SQL 快照，再把 Lyra 账户 UUID
 * 映射成旧账本键供既有确定性计算内核使用。输出通过 App 共用公开投影去除旧账户
 * 键、交易哈希和 Decimal 隐式序列化。
 *
 * @param database - 单一 TypeScript 服务持有的 DuckDB。
 */
export function createFinancialToolRegistry(
  database: LyraDatabase,
  marketData: MarketDataReader = new MarketDataService({
    providers: [], routes: { stock: [], option: [] }, policies: {},
  }),
): FinancialToolRegistry {
  return new FinancialToolRegistry({
    accounts: async ({ context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      const accountIds = new Set(prepared.scope.accountIds);
      const rows = createPublicAccountList(
        prepared.snapshot.accounts.filter((account) => accountIds.has(account.id)),
      );
      return {
        rows,
        count: rows.length,
        currencyGroups: copyCurrencyGroups(prepared.scope),
      };
    },
    summary: async ({ context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      return createAgentSummary(prepared.snapshot, prepared.scope);
    },
    positions: async ({ arguments: arguments_, context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      const rows = createPublicPositionList(
        aggregatePositions(
          prepared.snapshot.openLots,
          { kind: "all" },
          optionalString(arguments_, "underlying"),
        ),
        prepared.scope,
      );
      return {
        rows,
        count: rows.length,
        basis: "historical_cost_not_market_value" as const,
        currencyGroups: copyCurrencyGroups(prepared.scope),
      };
    },
    expiring: async ({ arguments: arguments_, context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      const result = analyzeExpiringPositions(
        aggregatePositions(prepared.snapshot.openLots, { kind: "all" }),
        utcDateFromTimestamp(context.asOf),
        requiredInteger(arguments_, "days"),
      );
      return {
        ...createPublicExpiringPositions(result, prepared.scope),
        basis: "historical_premium_not_market_value" as const,
        currencyGroups: copyCurrencyGroups(prepared.scope),
      };
    },
    trades: async ({ arguments: arguments_, context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      const result = queryTrades(
        prepared.snapshot.transactions,
        { kind: "all" },
        createTradeFilters(arguments_),
      );
      return createPublicTradeQuery(result, prepared.scope);
    },
    realized: async ({ arguments: arguments_, context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      const result = queryRealized(
        prepared.snapshot.realizedRecords,
        { kind: "all" },
        createRealizedFilters(arguments_),
      );
      return createPublicRealizedQuery(result, prepared.scope);
    },
    cashflow: async ({ arguments: arguments_, context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      const result = queryCashflow(
        prepared.snapshot.transactions,
        { kind: "all" },
        createCashflowFilters(arguments_),
      );
      return createPublicCashflowQuery(result, prepared.scope);
    },
    monthly: async ({ arguments: arguments_, context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      const fromDate = optionalString(arguments_, "fromDate");
      const toDate = optionalString(arguments_, "toDate");
      return createCurrencyGroupedResult(prepared, (group) => (
        calculateMonthlyPerformance(
          group.realizedRecords,
          { kind: "all" },
          fromDate,
          toDate,
        )
      ));
    },
    allocation: async ({ arguments: arguments_, context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      const groupBy = requiredString(arguments_, "groupBy") as AllocationGroupBy;
      return createCurrencyGroupedResult(prepared, (group) => (
        calculateAllocation(group.openLots, { kind: "all" }, groupBy)
      ));
    },
    attribution: async ({ arguments: arguments_, context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      const groupBy = requiredString(arguments_, "groupBy") as AttributionGroupBy;
      const fromDate = optionalString(arguments_, "fromDate");
      const toDate = optionalString(arguments_, "toDate");
      return createCurrencyGroupedResult(prepared, (group) => (
        calculateAttribution(
          group.realizedRecords,
          { kind: "all" },
          groupBy,
          fromDate,
          toDate,
        )
      ));
    },
    drawdown: async ({ arguments: arguments_, context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      const fromDate = optionalString(arguments_, "fromDate");
      const toDate = optionalString(arguments_, "toDate");
      return createCurrencyGroupedResult(prepared, (group) => (
        calculateDrawdown(group.realizedRecords, { kind: "all" }, fromDate, toDate)
      ));
    },
    risk: async ({ context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      const asOf = utcDateFromTimestamp(context.asOf);
      return createCurrencyGroupedResult(prepared, (group) => (
        calculateOptionRisk(group.openLots, { kind: "all" }, asOf)
      ));
    },
    "holding-period": async ({ context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      return createCurrencyGroupedResult(prepared, (group) => (
        calculateHoldingPeriod(group.realizedRecords, { kind: "all" })
      ));
    },
    extremes: async ({ arguments: arguments_, context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      const limit = requiredInteger(arguments_, "limit");
      return createCurrencyGroupedResult(prepared, (group) => projectExtremeTrades(
        calculateExtremeTrades(group.realizedRecords, { kind: "all" }, limit),
        prepared.scope,
      ));
    },
    streaks: async ({ context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      return createCurrencyGroupedResult(prepared, (group) => (
        calculateStreaks(group.realizedRecords, { kind: "all" })
      ));
    },
    stress: async ({ context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      return createCurrencyGroupedResult(prepared, (group) => (
        calculateOptionStress(group.openLots, { kind: "all" })
      ));
    },
    audit: async ({ context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      return auditLedger(
        prepared.snapshot.transactions,
        {
          openLots: prepared.snapshot.openLots,
          realizedRecords: prepared.snapshot.realizedRecords,
        },
        { kind: "all" },
      );
    },
    "market-positions": async ({ arguments: arguments_, context }) => {
      const prepared = await prepareAuthorizedSnapshot(database, context);
      const positions = aggregatePositions(
        prepared.snapshot.openLots,
        { kind: "all" },
        optionalString(arguments_, "underlying"),
      );
      return createMarketPositionView(positions, prepared.scope, marketData);
    },
  });
}

/** 已完成账户授权解析且只保留授权账本键的数据快照。 */
interface PreparedAuthorizedSnapshot {
  snapshot: LedgerAnalysisSnapshot;
  scope: ResolvedAccountScope;
}

/** 读取绑定版本，并在进入任何分析函数前按 UUID 权威范围过滤。 */
async function prepareAuthorizedSnapshot(
  database: LyraDatabase,
  context: AgentToolContext,
): Promise<PreparedAuthorizedSnapshot> {
  const snapshot = await loadLedgerAnalysisSnapshot(database, context.ledgerVersion);
  const scope = resolveAccountScope(context.accountScope, snapshot.accounts);
  const allowedKeys = new Set(scope.ledgerAccountKeys);
  return {
    scope,
    snapshot: {
      ...snapshot,
      transactions: snapshot.transactions.filter((row) => allowedKeys.has(row.account)),
      openLots: snapshot.openLots.filter((row) => allowedKeys.has(row.account)),
      realizedRecords: snapshot.realizedRecords.filter((row) => allowedKeys.has(row.account)),
    },
  };
}

/** 按基础币种分别执行既有 summary，避免跨币种金额相加。 */
function createAgentSummary(
  snapshot: LedgerAnalysisSnapshot,
  scope: ResolvedAccountScope,
): AgentAccountSummaryResult {
  return createCurrencyGroupedResult({ snapshot, scope }, (group) => {
    const summary = calculateAccountSummary(
      group.transactions,
      {
        openLots: group.openLots,
        realizedRecords: group.realizedRecords,
      },
      { kind: "all" },
    );
    return projectSummary(summary);
  });
}

/** 一个基础币种内可安全参与金额合计的账本子集。 */
interface CurrencyLedgerGroup {
  currency: string;
  accountIds: readonly string[];
  transactions: readonly ParsedTransaction[];
  openLots: readonly Lot[];
  realizedRecords: readonly RealizedRecord[];
}

/** 对每个币种子集执行同一确定性分析，并附加公开账户 UUID。 */
function createCurrencyGroupedResult<T>(
  prepared: PreparedAuthorizedSnapshot,
  calculate: (group: CurrencyLedgerGroup) => T,
): AgentCurrencyGroupedResult<T> {
  const groups = createCurrencyLedgerGroups(prepared.snapshot, prepared.scope).map((group) => ({
    currency: group.currency,
    accountIds: [...group.accountIds],
    data: calculate(group),
  }));
  return { aggregateCurrency: prepared.scope.aggregateCurrency, groups };
}

/** 把已授权账本按账户基础币种切分，供金额类分析阻止跨币种伪合计。 */
function createCurrencyLedgerGroups(
  snapshot: LedgerAnalysisSnapshot,
  scope: ResolvedAccountScope,
): readonly CurrencyLedgerGroup[] {
  const keyByAccountId = new Map(snapshot.accounts.map((account) => [
    account.id,
    account.legacyAccountKey,
  ]));
  return scope.currencyGroups.map((group) => {
    const accountKeys = new Set(group.accountIds.map((accountId) => {
      const key = keyByAccountId.get(accountId);
      if (key === undefined) {
        throw new Error("账户币种组与账本快照不一致");
      }
      return key;
    }));
    return {
      currency: group.currency,
      accountIds: [...group.accountIds],
      transactions: filterTransactions(snapshot.transactions, accountKeys),
      openLots: filterLots(snapshot.openLots, accountKeys),
      realizedRecords: filterRealized(snapshot.realizedRecords, accountKeys),
    };
  });
}

/** 显式移除只供领域筛选使用的旧账户范围。 */
function projectSummary(summary: AccountSummary): Omit<AccountSummary, "accountScope"> {
  return {
    netDeposits: summary.netDeposits,
    realizedPnlStock: summary.realizedPnlStock,
    realizedPnlOption: summary.realizedPnlOption,
    realizedPnlTotal: summary.realizedPnlTotal,
    openPositions: summary.openPositions,
    totalFees: summary.totalFees,
    dividends: summary.dividends,
    interest: summary.interest,
    taxes: summary.taxes,
    cashBalance: summary.cashBalance,
    transactionCount: summary.transactionCount,
    dateRange: summary.dateRange,
  };
}

/** `extremes` 只公开业务字段和 Decimal 文本，不公开交易哈希或旧账户键。 */
function projectExtremeTrades(
  result: ExtremeTradesResult,
  scope: ResolvedAccountScope,
): Readonly<Record<string, unknown>> {
  const accountIds = new Map(scope.ledgerAccountKeys.map((key, index) => [
    key,
    scope.accountIds[index],
  ]));
  return {
    rows: result.rows.map((row) => {
      const accountId = accountIds.get(row.trade.account);
      if (accountId === undefined) {
        throw new Error("最佳/最差交易包含授权范围外账户");
      }
      return {
        side: row.side,
        rank: row.rank,
        trade: {
          accountId,
          closeDate: row.trade.closeDate,
          symbolKey: row.trade.symbolKey,
          underlying: row.trade.underlying,
          assetType: row.trade.assetType,
          direction: row.trade.direction,
          closeAction: row.trade.closeAction,
          quantity: row.trade.quantity.toString(),
          pnl: row.trade.pnl.toString(),
          fees: row.trade.fees.toString(),
          earliestOpenDate: row.trade.earliestOpenDate,
          latestOpenDate: row.trade.latestOpenDate,
        },
      };
    }),
    perSide: result.perSide,
    basis: result.basis,
  };
}

/** 按一组旧账户键过滤交易。 */
function filterTransactions(
  rows: readonly ParsedTransaction[],
  accountKeys: ReadonlySet<string>,
): readonly ParsedTransaction[] {
  return rows.filter((row) => accountKeys.has(row.account));
}

/** 按一组旧账户键过滤未平批次。 */
function filterLots(rows: readonly Lot[], accountKeys: ReadonlySet<string>): readonly Lot[] {
  return rows.filter((row) => accountKeys.has(row.account));
}

/** 按一组旧账户键过滤已实现记录。 */
function filterRealized(
  rows: readonly RealizedRecord[],
  accountKeys: ReadonlySet<string>,
): readonly RealizedRecord[] {
  return rows.filter((row) => accountKeys.has(row.account));
}

/** 复制公开币种组，避免返回对象与解析结果共享可变数组。 */
function copyCurrencyGroups(scope: ResolvedAccountScope): ResolvedAccountScope["currencyGroups"] {
  return scope.currencyGroups.map((group) => ({
    currency: group.currency,
    accountIds: [...group.accountIds],
  }));
}

/** 从已经过工具 JSON Schema 校验的参数中读取可选字符串。 */
function optionalString(
  arguments_: Readonly<Record<string, unknown>>,
  name: string,
): string | undefined {
  const value = arguments_[name];
  if (value === undefined) {
    return undefined;
  }
  if (typeof value !== "string") {
    throw new RangeError(`${name} 必须是字符串`);
  }
  return value;
}

/** 从已经过工具 JSON Schema 校验的参数中读取必填字符串。 */
function requiredString(
  arguments_: Readonly<Record<string, unknown>>,
  name: string,
): string {
  const value = optionalString(arguments_, name);
  if (value === undefined) {
    throw new RangeError(`${name} 为必填字符串`);
  }
  return value;
}

/** 把金融工具参数映射为既有交易分页过滤器。 */
function createTradeFilters(
  arguments_: Readonly<Record<string, unknown>>,
): TradeQueryFilters {
  const symbol = optionalString(arguments_, "symbol");
  const action = optionalString(arguments_, "action");
  const sourceType = optionalString(arguments_, "sourceType");
  const fromDate = optionalString(arguments_, "fromDate");
  const toDate = optionalString(arguments_, "toDate");
  const limit = optionalInteger(arguments_, "limit");
  const cursor = optionalString(arguments_, "cursor");
  return {
    ...(symbol === undefined ? {} : { symbol }),
    ...(action === undefined ? {} : { action: action as TransactionAction }),
    ...(sourceType === undefined ? {} : { sourceType: sourceType as LedgerSourceType }),
    ...(fromDate === undefined ? {} : { fromDate }),
    ...(toDate === undefined ? {} : { toDate }),
    ...(limit === undefined ? {} : { limit }),
    ...(cursor === undefined ? {} : { cursor }),
  };
}

/** 把金融工具参数映射为既有已实现分页过滤器。 */
function createRealizedFilters(
  arguments_: Readonly<Record<string, unknown>>,
): RealizedQueryFilters {
  const symbol = optionalString(arguments_, "symbol");
  const fromDate = optionalString(arguments_, "fromDate");
  const toDate = optionalString(arguments_, "toDate");
  const limit = optionalInteger(arguments_, "limit");
  const cursor = optionalString(arguments_, "cursor");
  return {
    ...(symbol === undefined ? {} : { symbol }),
    ...(fromDate === undefined ? {} : { fromDate }),
    ...(toDate === undefined ? {} : { toDate }),
    ...(limit === undefined ? {} : { limit }),
    ...(cursor === undefined ? {} : { cursor }),
  };
}

/** 把金融工具参数映射为既有现金流水分页过滤器。 */
function createCashflowFilters(
  arguments_: Readonly<Record<string, unknown>>,
): CashflowQueryFilters {
  const sourceType = optionalString(arguments_, "sourceType");
  const fromDate = optionalString(arguments_, "fromDate");
  const toDate = optionalString(arguments_, "toDate");
  const limit = optionalInteger(arguments_, "limit");
  const cursor = optionalString(arguments_, "cursor");
  return {
    ...(sourceType === undefined ? {} : { sourceType: sourceType as LedgerSourceType }),
    ...(fromDate === undefined ? {} : { fromDate }),
    ...(toDate === undefined ? {} : { toDate }),
    ...(limit === undefined ? {} : { limit }),
    ...(cursor === undefined ? {} : { cursor }),
  };
}

/** 读取可选安全整数；范围继续由唯一工具 schema 与领域查询共同约束。 */
function optionalInteger(
  arguments_: Readonly<Record<string, unknown>>,
  name: string,
): number | undefined {
  const value = arguments_[name];
  if (value === undefined) {
    return undefined;
  }
  if (!Number.isSafeInteger(value)) {
    throw new RangeError(`${name} 必须是安全整数`);
  }
  return value as number;
}

/** 从已经过工具 JSON Schema 校验的参数中读取必填整数。 */
function requiredInteger(
  arguments_: Readonly<Record<string, unknown>>,
  name: string,
): number {
  const value = arguments_[name];
  if (!Number.isSafeInteger(value)) {
    throw new RangeError(`${name} 必须是安全整数`);
  }
  return value as number;
}

/** Agent 会话时间统一按 UTC 日历日进入到期分析。 */
function utcDateFromTimestamp(value: string): string {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) {
    throw new RangeError("Agent 数据截止时间无效");
  }
  return timestamp.toISOString().slice(0, 10);
}
