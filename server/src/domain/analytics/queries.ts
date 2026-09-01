import { createHash } from "node:crypto";

import { Decimal } from "decimal.js";

import type { RealizedRecord } from "../fifo/index.js";
import type {
  LedgerSourceType,
  ParsedTransaction,
  TransactionAction,
} from "../ledger/index.js";
import { isDateInRange, validateDateRange } from "./dates.js";
import { isAccountInScope } from "./scope.js";
import type {
  AccountScope,
  CashflowQueryResult,
  RealizedQueryResult,
  TradeQueryResult,
} from "./types.js";

/** 交易流水查询的可选过滤条件。 */
export interface TradeQueryFilters {
  symbol?: string;
  action?: TransactionAction;
  sourceType?: LedgerSourceType;
  fromDate?: string;
  toDate?: string;
  limit?: number;
  /** 上一页返回的不透明游标；只能在相同账户范围和筛选条件下复用。 */
  cursor?: string;
}

/** 已实现损益查询的可选过滤条件。 */
export interface RealizedQueryFilters {
  symbol?: string;
  fromDate?: string;
  toDate?: string;
  limit?: number;
  /** 上一页返回的不透明游标；只能在相同账户范围和筛选条件下复用。 */
  cursor?: string;
}

/** 现金流水查询的可选日期范围。 */
export interface CashflowQueryFilters {
  fromDate?: string;
  toDate?: string;
  sourceType?: LedgerSourceType;
  limit?: number;
  /** 上一页返回的不透明游标；只能在相同账户范围和筛选条件下复用。 */
  cursor?: string;
}

/**
 * 查询标准交易流水，复现 Python `trades` 的过滤和排序口径。
 *
 * @param transactions - 标准交易账本快照。
 * @param accountScope - 必填的单账户或全部账户范围。
 * @param filters - 标的、动作、闭区间日期和最大返回数；默认最多 50 条。
 * @returns 确定性排序的当前页交易和可选下一页游标。
 * @throws RangeError 当日期、limit 或游标无效时抛出。
 */
export function queryTrades(
  transactions: readonly ParsedTransaction[],
  accountScope: AccountScope,
  filters: TradeQueryFilters = {},
): TradeQueryResult {
  validateDateRange(filters.fromDate, filters.toDate);
  const limit = requirePageLimit(filters.limit);
  const symbol = filters.symbol?.toUpperCase();
  const filterFingerprint = createTradeFilterFingerprint(accountScope, {
    symbol,
    action: filters.action,
    sourceType: filters.sourceType,
    fromDate: filters.fromDate,
    toDate: filters.toDate,
  });
  const filtered = transactions
    .filter((transaction) => (
      isAccountInScope(transaction.account, accountScope)
      && (symbol === undefined
        || transaction.underlying === symbol
        || transaction.rawSymbol === symbol)
      && (filters.action === undefined || transaction.action === filters.action)
      && (filters.sourceType === undefined || transaction.sourceType === filters.sourceType)
      && isDateInRange(transaction.transactionDate, filters.fromDate, filters.toDate)
    ))
    .toSorted((left, right) => (
      right.transactionDate.localeCompare(left.transactionDate)
      || left.sequence - right.sequence
      || left.account.localeCompare(right.account)
      || left.sourceType.localeCompare(right.sourceType)
      || left.sourceReference.localeCompare(right.sourceReference)
      || left.occurrence - right.occurrence
      || left.transactionHash.localeCompare(right.transactionHash)
    ));
  let pageStart = 0;
  if (filters.cursor !== undefined) {
    const cursor = decodeTradeCursor(filters.cursor);
    if (cursor.filterFingerprint !== filterFingerprint) {
      throw new RangeError("交易流水游标与当前账户范围或筛选条件不一致");
    }
    const cursorIndex = filtered.findIndex((transaction) => (
      transaction.transactionHash === cursor.transactionHash
    ));
    if (cursorIndex === -1) {
      throw new RangeError("交易流水游标已不属于当前账本快照");
    }
    pageStart = cursorIndex + 1;
  }
  const pageCandidates = filtered.slice(pageStart, pageStart + limit + 1);
  const rows = pageCandidates.slice(0, limit);
  const lastRow = rows.at(-1);
  const nextCursor = pageCandidates.length > limit && lastRow !== undefined
    ? encodeTradeCursor({
        transactionHash: lastRow.transactionHash,
        filterFingerprint,
      })
    : null;

  return { rows, count: rows.length, nextCursor };
}

/** 交易游标只保存定位哈希和查询条件指纹，不复制金融数据。 */
interface TradeCursorPayload {
  version: 1;
  transactionHash: string;
  filterFingerprint: string;
}

/** 对账户范围与规范化筛选条件生成稳定指纹，阻止跨查询误用游标。 */
function createTradeFilterFingerprint(
  accountScope: AccountScope,
  filters: {
    symbol: string | undefined;
    action: TransactionAction | undefined;
    sourceType: LedgerSourceType | undefined;
    fromDate: string | undefined;
    toDate: string | undefined;
  },
): string {
  return createHash("sha256").update(JSON.stringify({
    accountScope: accountScope.kind === "all"
      ? { kind: "all" }
      : { kind: "account", account: accountScope.account },
    symbol: filters.symbol ?? null,
    action: filters.action ?? null,
    sourceType: filters.sourceType ?? null,
    fromDate: filters.fromDate ?? null,
    toDate: filters.toDate ?? null,
  })).digest("hex");
}

/** 将下一页定位信息编码为 URL 安全的不透明文本。 */
function encodeTradeCursor(input: Omit<TradeCursorPayload, "version">): string {
  return Buffer.from(JSON.stringify({ version: 1, ...input }), "utf8").toString("base64url");
}

/** 严格解码客户端游标；解析失败不得回退到第一页。 */
function decodeTradeCursor(value: string): TradeCursorPayload {
  if (value.length === 0 || value.length > 1_024) {
    throw new RangeError("交易流水游标无效");
  }
  try {
    const parsed = JSON.parse(Buffer.from(value, "base64url").toString("utf8")) as unknown;
    if (
      typeof parsed !== "object"
      || parsed === null
      || !("version" in parsed)
      || parsed.version !== 1
      || !("transactionHash" in parsed)
      || typeof parsed.transactionHash !== "string"
      || !/^[a-f0-9]{64}$/.test(parsed.transactionHash)
      || !("filterFingerprint" in parsed)
      || typeof parsed.filterFingerprint !== "string"
      || !/^[a-f0-9]{64}$/.test(parsed.filterFingerprint)
    ) {
      throw new RangeError("交易流水游标结构无效");
    }
    return parsed as TradeCursorPayload;
  } catch (error) {
    if (error instanceof RangeError) {
      throw error;
    }
    throw new RangeError("交易流水游标无效", { cause: error });
  }
}

/** 已实现与现金流分页共用的定位信封；rowKey 只保存不可逆哈希。 */
interface DetailCursorPayload {
  version: 1;
  query: "realized" | "cashflow";
  rowKey: string;
  filterFingerprint: string;
}

/** 生成绑定查询类型、账户范围和规范筛选条件的摘要。 */
function createDetailFilterFingerprint(
  query: DetailCursorPayload["query"],
  accountScope: AccountScope,
  filters: Readonly<Record<string, string | null>>,
): string {
  return createHash("sha256").update(JSON.stringify({
    query,
    accountScope: accountScope.kind === "all"
      ? { kind: "all" }
      : { kind: "account", account: accountScope.account },
    ...filters,
  })).digest("hex");
}

/** 编码不含金融字段的 URL 安全明细游标。 */
function encodeDetailCursor(input: Omit<DetailCursorPayload, "version">): string {
  return Buffer.from(JSON.stringify({ version: 1, ...input }), "utf8").toString("base64url");
}

/** 严格解码并校验查询类型，禁止跨 realized/cashflow 复用。 */
function decodeDetailCursor(
  value: string,
  query: DetailCursorPayload["query"],
  label: string,
): DetailCursorPayload {
  if (value.length === 0 || value.length > 1_024) {
    throw new RangeError(`${label}游标无效`);
  }
  try {
    const parsed = JSON.parse(Buffer.from(value, "base64url").toString("utf8")) as unknown;
    if (
      typeof parsed !== "object"
      || parsed === null
      || !("version" in parsed)
      || parsed.version !== 1
      || !("query" in parsed)
      || parsed.query !== query
      || !("rowKey" in parsed)
      || typeof parsed.rowKey !== "string"
      || !/^[a-f0-9]{64}$/.test(parsed.rowKey)
      || !("filterFingerprint" in parsed)
      || typeof parsed.filterFingerprint !== "string"
      || !/^[a-f0-9]{64}$/.test(parsed.filterFingerprint)
    ) {
      throw new RangeError(`${label}游标结构无效`);
    }
    return parsed as DetailCursorPayload;
  } catch (error) {
    if (error instanceof RangeError) {
      throw error;
    }
    throw new RangeError(`${label}游标无效`, { cause: error });
  }
}

/**
 * 查询 FIFO 已实现明细，并返回筛选范围内的损益合计。
 *
 * @param records - 与当前账本版本对应的 FIFO 已实现记录。
 * @param accountScope - 必填的单账户或全部账户范围。
 * @param filters - 标的或合约、平仓日期闭区间。
 * @returns 平仓日倒序的明细及四位小数损益合计。
 * @throws RangeError 当日期无效或范围反向时抛出。
 */
export function queryRealized(
  records: readonly RealizedRecord[],
  accountScope: AccountScope,
  filters: RealizedQueryFilters = {},
): RealizedQueryResult {
  validateDateRange(filters.fromDate, filters.toDate);
  const limit = requirePageLimit(filters.limit);
  const symbol = filters.symbol?.toUpperCase();
  const filterFingerprint = createDetailFilterFingerprint("realized", accountScope, {
    symbol: symbol ?? null,
    fromDate: filters.fromDate ?? null,
    toDate: filters.toDate ?? null,
  });
  const filtered = records
    .filter((record) => (
      isAccountInScope(record.account, accountScope)
      && (symbol === undefined || record.underlying === symbol || record.symbolKey === symbol)
      && isDateInRange(record.closeDate, filters.fromDate, filters.toDate)
    ))
    .toSorted((left, right) => (
      right.closeDate.localeCompare(left.closeDate)
      || left.closeTransaction.localeCompare(right.closeTransaction)
      || left.openTransaction.localeCompare(right.openTransaction)
      || left.account.localeCompare(right.account)
    ));
  let pageStart = 0;
  if (filters.cursor !== undefined) {
    const cursor = decodeDetailCursor(filters.cursor, "realized", "已实现明细");
    if (cursor.filterFingerprint !== filterFingerprint) {
      throw new RangeError("已实现明细游标与当前账户范围或筛选条件不一致");
    }
    const cursorIndex = filtered.findIndex((record) => (
      createRealizedRowKey(record) === cursor.rowKey
    ));
    if (cursorIndex === -1) {
      throw new RangeError("已实现明细游标已不属于当前账本快照");
    }
    pageStart = cursorIndex + 1;
  }
  const pageCandidates = filtered.slice(pageStart, pageStart + limit + 1);
  const rows = pageCandidates.slice(0, limit);
  const lastRow = rows.at(-1);
  const nextCursor = pageCandidates.length > limit && lastRow !== undefined
    ? encodeDetailCursor({
        query: "realized",
        rowKey: createRealizedRowKey(lastRow),
        filterFingerprint,
      })
    : null;

  return {
    rows,
    count: rows.length,
    totalCount: filtered.length,
    totalPnl: sumDecimals(filtered.map((row) => row.pnl)).toFixed(4),
    nextCursor,
  };
}

/** 已实现记录没有独立 ID，使用构成 FIFO 匹配身份的交易哈希生成定位键。 */
function createRealizedRowKey(record: RealizedRecord): string {
  return createHash("sha256").update(JSON.stringify({
    account: record.account,
    openTransaction: record.openTransaction,
    closeTransaction: record.closeTransaction,
  })).digest("hex");
}

/**
 * 查询现金类交易，并按 Schwab Action 汇总现金变动。
 *
 * @param transactions - 标准交易账本快照。
 * @param accountScope - 必填的单账户或全部账户范围。
 * @param filters - 交易日期闭区间。
 * @returns 日期倒序明细、总额和分动作四位小数合计。
 * @throws RangeError 当日期无效或范围反向时抛出。
 */
export function queryCashflow(
  transactions: readonly ParsedTransaction[],
  accountScope: AccountScope,
  filters: CashflowQueryFilters = {},
): CashflowQueryResult {
  validateDateRange(filters.fromDate, filters.toDate);
  const limit = requirePageLimit(filters.limit);
  const filterFingerprint = createDetailFilterFingerprint("cashflow", accountScope, {
    sourceType: filters.sourceType ?? null,
    fromDate: filters.fromDate ?? null,
    toDate: filters.toDate ?? null,
  });
  const filtered = transactions
    .filter((transaction) => (
      transaction.assetType === "cash"
      && isAccountInScope(transaction.account, accountScope)
      && (filters.sourceType === undefined || transaction.sourceType === filters.sourceType)
      && isDateInRange(transaction.transactionDate, filters.fromDate, filters.toDate)
    ))
    .toSorted((left, right) => (
      right.transactionDate.localeCompare(left.transactionDate)
      || left.sequence - right.sequence
      || left.account.localeCompare(right.account)
      || left.sourceType.localeCompare(right.sourceType)
      || left.sourceReference.localeCompare(right.sourceReference)
      || left.occurrence - right.occurrence
      || left.transactionHash.localeCompare(right.transactionHash)
    ));
  let pageStart = 0;
  if (filters.cursor !== undefined) {
    const cursor = decodeDetailCursor(filters.cursor, "cashflow", "现金流水");
    if (cursor.filterFingerprint !== filterFingerprint) {
      throw new RangeError("现金流水游标与当前账户范围或筛选条件不一致");
    }
    const cursorIndex = filtered.findIndex((transaction) => (
      transaction.transactionHash === cursor.rowKey
    ));
    if (cursorIndex === -1) {
      throw new RangeError("现金流水游标已不属于当前账本快照");
    }
    pageStart = cursorIndex + 1;
  }
  const pageCandidates = filtered.slice(pageStart, pageStart + limit + 1);
  const rows = pageCandidates.slice(0, limit);
  const lastRow = rows.at(-1);
  const nextCursor = pageCandidates.length > limit && lastRow !== undefined
    ? encodeDetailCursor({
        query: "cashflow",
        rowKey: lastRow.transactionHash,
        filterFingerprint,
      })
    : null;
  const actionTotals = new Map<string, Decimal>();
  for (const row of filtered) {
    if (row.amount === null) {
      continue;
    }
    actionTotals.set(row.action, (actionTotals.get(row.action) ?? new Decimal(0)).plus(row.amount));
  }

  return {
    rows,
    count: rows.length,
    totalCount: filtered.length,
    total: sumDecimals(filtered.map((row) => row.amount)).toFixed(4),
    byAction: Object.fromEntries(
      [...actionTotals.entries()].map(([action, amount]) => [action, amount.toFixed(4)]),
    ),
    nextCursor,
  };
}

/** 三类明细查询共用的页长限制。 */
function requirePageLimit(value: number | undefined): number {
  const limit = value ?? 50;
  if (!Number.isSafeInteger(limit) || limit <= 0 || limit > 200) {
    throw new RangeError("limit 必须是 1 到 200 的安全整数");
  }
  return limit;
}

/** 合计可空 Decimal；空字段与 SQL `sum` 一样不贡献数值。 */
function sumDecimals(values: readonly (Decimal | null)[]): Decimal {
  return values.reduce<Decimal>(
    (total, value) => value === null ? total : total.plus(value),
    new Decimal(0),
  );
}
