import { Decimal } from "decimal.js";

import type { Lot, RealizedRecord } from "../fifo/index.js";
import { MILLISECONDS_PER_DAY, parseIsoDate } from "./dates.js";
import { aggregatePositions } from "./positions.js";
import { isAccountInScope } from "./scope.js";
import type {
  AccountScope,
  ClosedTransaction,
  ExtremeTradesResult,
  HoldingPeriodResult,
  OptionStressResult,
  StreakRow,
  StreaksResult,
} from "./types.js";

/** 持有周期桶的固定顺序。 */
const HOLDING_BUCKET_ORDER = ["0-7d", "8-30d", "31-90d", "90d+"] as const;

/**
 * 把一笔平仓跨越的多个 FIFO realized 行重新聚合为一笔交易。
 *
 * @param records - FIFO 已实现明细。
 * @param accountScope - 显式账户范围。
 * @returns 按平仓日期、交易哈希稳定排序的平仓交易。
 */
export function aggregateClosedTransactions(
  records: readonly RealizedRecord[],
  accountScope: AccountScope,
): readonly ClosedTransaction[] {
  const groups = new Map<string, ClosedTransaction>();

  for (const record of records) {
    if (!isAccountInScope(record.account, accountScope)) {
      continue;
    }
    const key = JSON.stringify([
      record.closeTransaction,
      record.account,
      record.closeDate,
      record.symbolKey,
      record.underlying,
      record.assetType,
      record.direction,
      record.closeAction,
    ]);
    const existing = groups.get(key);
    if (existing === undefined) {
      groups.set(key, {
        closeTransaction: record.closeTransaction,
        account: record.account,
        closeDate: record.closeDate,
        symbolKey: record.symbolKey,
        underlying: record.underlying,
        assetType: record.assetType,
        direction: record.direction,
        closeAction: record.closeAction,
        quantity: record.quantity,
        pnl: record.pnl,
        fees: record.openFees.plus(record.closeFees),
        earliestOpenDate: record.openDate,
        latestOpenDate: record.openDate,
      });
      continue;
    }
    existing.quantity = existing.quantity.plus(record.quantity);
    existing.pnl = existing.pnl.plus(record.pnl);
    existing.fees = existing.fees.plus(record.openFees).plus(record.closeFees);
    if (record.openDate < existing.earliestOpenDate) {
      existing.earliestOpenDate = record.openDate;
    }
    if (record.openDate > existing.latestOpenDate) {
      existing.latestOpenDate = record.openDate;
    }
  }

  return [...groups.values()].toSorted((left, right) => (
    left.closeDate.localeCompare(right.closeDate)
    || left.closeTransaction.localeCompare(right.closeTransaction)
  ));
}

/**
 * 按 FIFO lot 的实际持有天数区间汇总已实现绩效。
 *
 * @param records - FIFO 已实现明细。
 * @param accountScope - 显式账户范围。
 * @returns 四个固定持有期桶中实际存在的结果。
 */
export function calculateHoldingPeriod(
  records: readonly RealizedRecord[],
  accountScope: AccountScope,
): HoldingPeriodResult {
  const groups = new Map<typeof HOLDING_BUCKET_ORDER[number], Array<{
    record: RealizedRecord;
    holdingDays: number;
  }>>();

  for (const record of records) {
    if (!isAccountInScope(record.account, accountScope)) {
      continue;
    }
    const holdingDays = Math.round(
      (parseIsoDate(record.closeDate).getTime() - parseIsoDate(record.openDate).getTime())
      / MILLISECONDS_PER_DAY,
    );
    const bucket = holdingBucket(holdingDays);
    const group = groups.get(bucket) ?? [];
    group.push({ record, holdingDays });
    groups.set(bucket, group);
  }

  const rows = HOLDING_BUCKET_ORDER
    .filter((bucket) => groups.has(bucket))
    .map((bucket) => {
      const group = groups.get(bucket);
      if (group === undefined) {
        throw new Error(`持有周期桶 ${bucket} 缺少聚合数据`);
      }
      const wins = group.filter(({ record }) => record.pnl.greaterThan(0)).length;
      const losses = group.filter(({ record }) => record.pnl.lessThan(0)).length;
      const decisive = wins + losses;
      const netPnl = group.reduce((total, { record }) => total.plus(record.pnl), new Decimal(0));
      const averageHoldingDays = new Decimal(
        group.reduce((total, item) => total + item.holdingDays, 0),
      ).div(group.length).toDecimalPlaces(2, Decimal.ROUND_HALF_UP);
      const averageLotPnl = netPnl.div(group.length)
        .toDecimalPlaces(4, Decimal.ROUND_HALF_UP);

      return {
        bucket,
        closedLots: group.length,
        closeTransactions: new Set(group.map(({ record }) => record.closeTransaction)).size,
        wins,
        losses,
        winRatePct: decisive === 0
          ? null
          : new Decimal(wins).mul(100).div(decisive)
            .toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN).toFixed(2),
        averageHoldingDays: averageHoldingDays.toFixed(2),
        averageLotPnl: averageLotPnl.toFixed(4),
        netPnl: netPnl.toFixed(4),
      };
    });

  return { rows, buckets: rows.length, basis: "realized_fifo_lots" };
}

/**
 * 按平仓交易净损益列出最佳和最差各 N 笔。
 *
 * @param records - FIFO 已实现明细。
 * @param accountScope - 显式账户范围。
 * @param limit - 每一侧返回数量，必须大于零。
 */
export function calculateExtremeTrades(
  records: readonly RealizedRecord[],
  accountScope: AccountScope,
  limit: number,
): ExtremeTradesResult {
  if (!Number.isInteger(limit) || limit <= 0) {
    throw new RangeError("limit 必须是大于 0 的整数");
  }
  const trades = aggregateClosedTransactions(records, accountScope);
  const best = trades.toSorted((left, right) => (
    right.pnl.comparedTo(left.pnl)
    || left.closeDate.localeCompare(right.closeDate)
    || left.closeTransaction.localeCompare(right.closeTransaction)
  )).slice(0, limit);
  const worst = trades.toSorted((left, right) => (
    left.pnl.comparedTo(right.pnl)
    || left.closeDate.localeCompare(right.closeDate)
    || left.closeTransaction.localeCompare(right.closeTransaction)
  )).slice(0, limit);

  return {
    rows: [
      ...best.map((trade, index) => ({ side: "best" as const, rank: index + 1, trade })),
      ...worst.map((trade, index) => ({ side: "worst" as const, rank: index + 1, trade })),
    ],
    perSide: limit,
    basis: "aggregated_close_transaction",
  };
}

/**
 * 按平仓交易日期和哈希顺序统计最长连续盈利与连续亏损。
 *
 * 零损益交易会结束当前连续区间，但不会计入盈利或亏损序列。
 */
export function calculateStreaks(
  records: readonly RealizedRecord[],
  accountScope: AccountScope,
): StreaksResult {
  const trades = aggregateClosedTransactions(records, accountScope);
  const longest: Partial<Record<"win" | "loss", StreakRow>> = {};
  let current: StreakRow | null = null;
  let neutralTransactions = 0;

  const finishCurrent = (): void => {
    if (current === null) {
      return;
    }
    const previous = longest[current.kind];
    if (previous === undefined || current.count > previous.count) {
      longest[current.kind] = { ...current };
    }
  };

  for (const trade of trades) {
    const kind = trade.pnl.greaterThan(0) ? "win" : trade.pnl.lessThan(0) ? "loss" : null;
    if (kind === null) {
      neutralTransactions += 1;
      finishCurrent();
      current = null;
      continue;
    }
    if (current === null || current.kind !== kind) {
      finishCurrent();
      current = {
        kind,
        count: 1,
        startDate: trade.closeDate,
        endDate: trade.closeDate,
        pnl: trade.pnl.toFixed(4),
      };
      continue;
    }
    current.count += 1;
    current.endDate = trade.closeDate;
    current.pnl = new Decimal(current.pnl).plus(trade.pnl).toFixed(4);
  }
  finishCurrent();

  return {
    rows: [longest.win, longest.loss].filter((row): row is StreakRow => row !== undefined),
    closeTransactions: trades.length,
    neutralTransactions,
    basis: "aggregated_close_transaction",
  };
}

/**
 * 模拟当前全部长期权权利金归零的历史成本压力。
 *
 * @param lots - FIFO 未平批次。
 * @param accountScope - 显式账户范围。
 * @returns 按标的的最大已支付权利金损失，不包含市场数据。
 */
export function calculateOptionStress(
  lots: readonly Lot[],
  accountScope: AccountScope,
): OptionStressResult {
  const positions = aggregatePositions(lots, accountScope)
    .filter((position) => position.assetType === "option" && position.direction === "long");
  const groups = new Map<string | null, {
    contracts: Decimal;
    premiumAtRisk: Decimal;
    nearestExpiry: string | null;
    farthestExpiry: string | null;
  }>();

  for (const position of positions) {
    const existing = groups.get(position.underlying) ?? {
      contracts: new Decimal(0),
      premiumAtRisk: new Decimal(0),
      nearestExpiry: null,
      farthestExpiry: null,
    };
    existing.contracts = existing.contracts.plus(position.quantity);
    existing.premiumAtRisk = existing.premiumAtRisk.plus(position.cost.mul(100));
    if (position.expiry !== null
      && (existing.nearestExpiry === null || position.expiry < existing.nearestExpiry)) {
      existing.nearestExpiry = position.expiry;
    }
    if (position.expiry !== null
      && (existing.farthestExpiry === null || position.expiry > existing.farthestExpiry)) {
      existing.farthestExpiry = position.expiry;
    }
    groups.set(position.underlying, existing);
  }

  const sorted = [...groups.entries()].toSorted((left, right) => (
    right[1].premiumAtRisk.comparedTo(left[1].premiumAtRisk)
    || (left[0] ?? "").localeCompare(right[0] ?? "")
  ));
  const total = sorted.reduce(
    (sum, [, group]) => sum.plus(group.premiumAtRisk),
    new Decimal(0),
  ).toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN);
  const rows = sorted.map(([underlying, group]) => {
    const premiumAtRisk = group.premiumAtRisk.toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN);
    return {
      underlying,
      contracts: group.contracts.toFixed(6),
      premiumAtRisk: premiumAtRisk.toFixed(2),
      lossIfZero: premiumAtRisk.negated().toFixed(2),
      portfolioPremiumPct: total.equals(0)
        ? null
        : premiumAtRisk.mul(100).div(total)
          .toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN).toFixed(2),
      nearestExpiry: group.nearestExpiry,
      farthestExpiry: group.farthestExpiry,
    };
  });
  const topThree = sorted.slice(0, 3).reduce(
    (sum, [, group]) => sum.plus(group.premiumAtRisk),
    new Decimal(0),
  ).toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN);
  const totalContracts = sorted.reduce(
    (sum, [, group]) => sum.plus(group.contracts),
    new Decimal(0),
  );

  return {
    rows,
    longOptionContracts: totalContracts.toFixed(6),
    allLongOptionsLossIfZero: total.negated().toFixed(2),
    topThreeLossIfZero: topThree.negated().toFixed(2),
    basis: "maximum_paid_premium_loss_no_market_data",
  };
}

/** 将持有天数映射到固定区间。 */
function holdingBucket(days: number): typeof HOLDING_BUCKET_ORDER[number] {
  if (days <= 7) {
    return "0-7d";
  }
  if (days <= 30) {
    return "8-30d";
  }
  if (days <= 90) {
    return "31-90d";
  }
  return "90d+";
}

