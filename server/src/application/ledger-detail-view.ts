import type {
  CashflowQueryResult,
  ExpiringPositionsResult,
  PositionRow,
  RealizedQueryResult,
  TradeQueryResult,
} from "../domain/analytics/index.js";
import type { ParsedTransaction } from "../domain/ledger/index.js";
import type { ResolvedAccountScope } from "./account-scope.js";

/** App 可展示的当前持仓；所有精确数值均为十进制字符串。 */
export interface PublicPosition {
  accountId: string;
  symbolKey: string | null;
  assetType: "stock" | "option";
  underlying: string | null;
  expiry: string | null;
  strike: string | null;
  optionType: "C" | "P" | null;
  direction: "long" | "short";
  quantity: string;
  cost: string;
  firstOpenDate: string;
}

/** 到期页在公开持仓基础上增加的确定性状态。 */
export interface PublicExpiringPosition extends PublicPosition {
  status: "expired" | "expires_today" | "upcoming";
  daysRemaining: number;
  premiumDollars: string;
}

/** App 可展示的到期持仓结果。 */
export interface PublicExpiringPositionsResult {
  rows: readonly PublicExpiringPosition[];
  count: number;
  asOf: string;
  cutoff: string;
  expired: number;
  expiresToday: number;
  upcoming: number;
}

/** App 可展示的账本交易，不包含来源引用、摘要或内部物理顺序。 */
export interface PublicLedgerTransaction {
  accountId: string;
  recordDate: string;
  transactionDate: string;
  action: ParsedTransaction["action"];
  symbol: string | null;
  description: string | null;
  assetType: ParsedTransaction["assetType"];
  underlying: string | null;
  expiry: string | null;
  strike: string | null;
  optionType: "C" | "P" | null;
  quantity: string | null;
  price: string | null;
  fees: string | null;
  amount: string | null;
  sourceType: ParsedTransaction["sourceType"];
}

/** App 交易分页结果。 */
export interface PublicTradeQueryResult {
  rows: readonly PublicLedgerTransaction[];
  count: number;
  nextCursor: string | null;
}

/** App 可展示的 FIFO 已实现明细，不包含开平仓交易哈希。 */
export interface PublicRealizedRow {
  accountId: string;
  symbolKey: string | null;
  assetType: "stock" | "option";
  underlying: string | null;
  direction: "long" | "short";
  openDate: string;
  closeDate: string;
  quantity: string;
  openPrice: string;
  closePrice: string;
  openFees: string;
  closeFees: string;
  pnl: string;
  closeAction: import("../domain/ledger/index.js").PositionAction;
}

/** App 已实现明细分页结果。 */
export interface PublicRealizedQueryResult {
  rows: readonly PublicRealizedRow[];
  count: number;
  totalCount: number;
  totalPnl: string;
  nextCursor: string | null;
}

/** App 现金流水分页结果。 */
export interface PublicCashflowQueryResult {
  rows: readonly PublicLedgerTransaction[];
  count: number;
  totalCount: number;
  total: string;
  byAction: Readonly<Record<string, string>>;
  nextCursor: string | null;
}

/** 将当前持仓显式投影为 App 契约，不依赖 Decimal 的隐式 JSON 行为。 */
export function createPublicPositionList(
  rows: readonly PositionRow[],
  scope: ResolvedAccountScope,
): readonly PublicPosition[] {
  const accountIds = createAccountIdMap(scope);
  return rows.map((row) => projectPosition(row, accountIds));
}

/** 将到期持仓及统计显式投影为 App 契约。 */
export function createPublicExpiringPositions(
  result: ExpiringPositionsResult,
  scope: ResolvedAccountScope,
): PublicExpiringPositionsResult {
  const accountIds = createAccountIdMap(scope);
  return {
    rows: result.rows.map((row) => ({
      ...projectPosition(row, accountIds),
      status: row.status,
      daysRemaining: row.daysRemaining,
      premiumDollars: row.premiumDollars.toString(),
    })),
    count: result.count,
    asOf: result.asOf,
    cutoff: result.cutoff,
    expired: result.expired,
    expiresToday: result.expiresToday,
    upcoming: result.upcoming,
  };
}

/** 将交易流水分页结果投影为不含内部哈希和来源引用的 App 契约。 */
export function createPublicTradeQuery(
  result: TradeQueryResult,
  scope: ResolvedAccountScope,
): PublicTradeQueryResult {
  const accountIds = createAccountIdMap(scope);
  return {
    rows: result.rows.map((row) => projectTransaction(row, accountIds)),
    count: result.count,
    nextCursor: result.nextCursor,
  };
}

/** 将 FIFO 已实现分页结果投影为精确字符串且隐藏交易哈希。 */
export function createPublicRealizedQuery(
  result: RealizedQueryResult,
  scope: ResolvedAccountScope,
): PublicRealizedQueryResult {
  const accountIds = createAccountIdMap(scope);
  return {
    rows: result.rows.map((row) => ({
      accountId: requireAccountId(row.account, accountIds),
      symbolKey: row.symbolKey,
      assetType: row.assetType,
      underlying: row.underlying,
      direction: row.direction,
      openDate: row.openDate,
      closeDate: row.closeDate,
      quantity: row.quantity.toString(),
      openPrice: row.openPrice.toString(),
      closePrice: row.closePrice.toString(),
      openFees: row.openFees.toString(),
      closeFees: row.closeFees.toString(),
      pnl: row.pnl.toString(),
      closeAction: row.closeAction,
    })),
    count: result.count,
    totalCount: result.totalCount,
    totalPnl: result.totalPnl,
    nextCursor: result.nextCursor,
  };
}

/** 将现金流水分页结果投影为安全交易行，并复制分类合计。 */
export function createPublicCashflowQuery(
  result: CashflowQueryResult,
  scope: ResolvedAccountScope,
): PublicCashflowQueryResult {
  const accountIds = createAccountIdMap(scope);
  return {
    rows: result.rows.map((row) => projectTransaction(row, accountIds)),
    count: result.count,
    totalCount: result.totalCount,
    total: result.total,
    byAction: { ...result.byAction },
    nextCursor: result.nextCursor,
  };
}

/** 复用持仓字段投影，避免持仓页与到期页出现两套数值格式。 */
function projectPosition(
  row: PositionRow,
  accountIds: ReadonlyMap<string, string>,
): PublicPosition {
  return {
    accountId: requireAccountId(row.account, accountIds),
    symbolKey: row.symbolKey,
    assetType: row.assetType,
    underlying: row.underlying,
    expiry: row.expiry,
    strike: row.strike?.toString() ?? null,
    optionType: row.optionType,
    direction: row.direction,
    quantity: row.quantity.toString(),
    cost: row.cost.toString(),
    firstOpenDate: row.firstOpenDate,
  };
}

/** 逐字段复制公开交易，刻意不展开哈希、来源引用和 occurrence。 */
function projectTransaction(
  row: ParsedTransaction,
  accountIds: ReadonlyMap<string, string>,
): PublicLedgerTransaction {
  return {
    accountId: requireAccountId(row.account, accountIds),
    recordDate: row.recordDate,
    transactionDate: row.transactionDate,
    action: row.action,
    symbol: row.rawSymbol,
    description: row.description,
    assetType: row.assetType,
    underlying: row.underlying,
    expiry: row.expiry,
    strike: row.strike?.toString() ?? null,
    optionType: row.optionType,
    quantity: row.quantity?.toString() ?? null,
    price: row.price?.toString() ?? null,
    fees: row.fees?.toString() ?? null,
    amount: row.amount?.toString() ?? null,
    sourceType: row.sourceType,
  };
}

/** scope 中两个稳定同序数组形成旧账户键到 Lyra UUID 的唯一映射。 */
function createAccountIdMap(
  scope: ResolvedAccountScope,
): ReadonlyMap<string, string> {
  if (scope.ledgerAccountKeys.length !== scope.accountIds.length) {
    throw new Error("账本账户键与 Lyra 账户 ID 数量不一致");
  }
  const result = new Map<string, string>();
  scope.ledgerAccountKeys.forEach((ledgerKey, index) => {
    const accountId = scope.accountIds[index];
    if (accountId === undefined || result.has(ledgerKey)) {
      throw new Error("账本账户范围无法建立唯一公开映射");
    }
    result.set(ledgerKey, accountId);
  });
  return result;
}

/** 查询结果只能包含已经通过服务端账户范围授权的旧账户键。 */
function requireAccountId(
  ledgerAccountKey: string,
  accountIds: ReadonlyMap<string, string>,
): string {
  const accountId = accountIds.get(ledgerAccountKey);
  if (accountId === undefined) {
    throw new Error("账本查询结果包含授权范围外账户");
  }
  return accountId;
}
