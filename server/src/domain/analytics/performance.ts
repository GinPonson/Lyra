import { Decimal } from "decimal.js";

import type { Lot, RealizedRecord } from "../fifo/index.js";
import { isDateInRange, validateDateRange } from "./dates.js";
import { aggregatePositions } from "./positions.js";
import { isAccountInScope } from "./scope.js";
import type {
  AccountScope,
  AllocationGroupBy,
  AllocationResult,
  AttributionGroupBy,
  AttributionResult,
  MonthlyPerformanceResult,
} from "./types.js";

/**
 * 按平仓月份汇总 FIFO 已实现绩效。
 *
 * @param records - FIFO 已实现明细。
 * @param accountScope - 显式账户范围。
 * @param fromDate - 可选起始平仓日期，闭区间。
 * @param toDate - 可选截止平仓日期，闭区间。
 * @returns 月度胜率、盈亏比、净损益与费用。
 * @throws RangeError 当日期无效或范围反向时抛出。
 */
export function calculateMonthlyPerformance(
  records: readonly RealizedRecord[],
  accountScope: AccountScope,
  fromDate?: string,
  toDate?: string,
): MonthlyPerformanceResult {
  validateDateRange(fromDate, toDate);
  const groups = new Map<string, RealizedRecord[]>();

  for (const record of records) {
    if (!isAccountInScope(record.account, accountScope)
      || !isDateInRange(record.closeDate, fromDate, toDate)) {
      continue;
    }
    const month = record.closeDate.slice(0, 7);
    const group = groups.get(month) ?? [];
    group.push(record);
    groups.set(month, group);
  }

  const rows = [...groups.entries()]
    .toSorted(([left], [right]) => left.localeCompare(right))
    .map(([month, monthlyRecords]) => {
      const wins = monthlyRecords.filter((record) => record.pnl.greaterThan(0)).length;
      const losses = monthlyRecords.filter((record) => record.pnl.lessThan(0)).length;
      const decisive = wins + losses;
      const grossProfit = sumDecimals(
        monthlyRecords.filter((record) => record.pnl.greaterThan(0)).map((record) => record.pnl),
      );
      const grossLoss = sumDecimals(
        monthlyRecords.filter((record) => record.pnl.lessThan(0)).map((record) => record.pnl),
      );
      const netPnl = sumDecimals(monthlyRecords.map((record) => record.pnl));
      const fees = sumDecimals(monthlyRecords.map((record) => record.openFees.plus(record.closeFees)));

      return {
        month,
        closedLots: monthlyRecords.length,
        closeTransactions: new Set(monthlyRecords.map((record) => record.closeTransaction)).size,
        wins,
        losses,
        winRatePct: decisive === 0
          ? null
          : new Decimal(wins).mul(100).div(decisive)
            .toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN).toFixed(2),
        grossProfit: grossProfit.toFixed(4),
        grossLoss: grossLoss.toFixed(4),
        profitFactor: grossLoss.lessThan(0)
          ? grossProfit.div(grossLoss.abs())
            .toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN).toFixed(2)
          : null,
        netPnl: netPnl.toFixed(4),
        fees: fees.toFixed(6),
      };
    });

  return {
    rows,
    months: rows.length,
    totalPnl: sumDecimals(rows.map((row) => new Decimal(row.netPnl))).toFixed(4),
    totalFees: sumDecimals(rows.map((row) => new Decimal(row.fees))).toFixed(6),
    basis: "realized_fifo",
  };
}

/**
 * 按标的或资产类型汇总当前未平仓历史成本配置。
 *
 * @param lots - FIFO 未平批次。
 * @param accountScope - 显式账户范围。
 * @param groupBy - `underlying` 或 `asset-type`。
 * @returns 历史成本及其绝对金额占比；结果不是实时市值配置。
 */
export function calculateAllocation(
  lots: readonly Lot[],
  accountScope: AccountScope,
  groupBy: AllocationGroupBy,
): AllocationResult {
  const positions = aggregatePositions(lots, accountScope);
  const groups = new Map<string | null, {
    positions: number;
    longPositions: number;
    shortPositions: number;
    costBasis: Decimal;
  }>();

  for (const position of positions) {
    const category = groupBy === "underlying" ? position.underlying : position.assetType;
    const existing = groups.get(category) ?? {
      positions: 0,
      longPositions: 0,
      shortPositions: 0,
      costBasis: new Decimal(0),
    };
    existing.positions += 1;
    existing.longPositions += position.direction === "long" ? 1 : 0;
    existing.shortPositions += position.direction === "short" ? 1 : 0;
    existing.costBasis = existing.costBasis.plus(
      position.cost.abs().mul(position.assetType === "option" ? 100 : 1),
    );
    groups.set(category, existing);
  }

  const rawTotalCost = sumDecimals([...groups.values()].map((group) => group.costBasis));
  const rows = [...groups.entries()]
    .map(([category, group]) => ({
      category,
      positions: group.positions,
      longPositions: group.longPositions,
      shortPositions: group.shortPositions,
      costBasis: group.costBasis.toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN).toFixed(2),
      allocationPct: rawTotalCost.equals(0)
        ? null
        : group.costBasis.mul(100).div(rawTotalCost)
          .toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN).toFixed(2),
      rawCostBasis: group.costBasis,
    }))
    .toSorted((left, right) => (
      right.rawCostBasis.comparedTo(left.rawCostBasis)
      || (left.category ?? "").localeCompare(right.category ?? "")
    ))
    .map(({ rawCostBasis: _rawCostBasis, ...row }) => row);

  return {
    rows,
    groups: rows.length,
    totalCostBasis: rawTotalCost.toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN).toFixed(2),
    basis: "historical_cost_not_market_value",
  };
}

/**
 * 按指定维度拆解 FIFO 已实现损益贡献。
 *
 * @param records - FIFO 已实现明细。
 * @param accountScope - 显式账户范围。
 * @param groupBy - 标的、资产类型、方向或平仓动作。
 * @param fromDate - 可选起始平仓日期。
 * @param toDate - 可选截止平仓日期。
 * @returns 分组净损益、胜率、费用和绝对贡献占比。
 */
export function calculateAttribution(
  records: readonly RealizedRecord[],
  accountScope: AccountScope,
  groupBy: AttributionGroupBy,
  fromDate?: string,
  toDate?: string,
): AttributionResult {
  validateDateRange(fromDate, toDate);
  const groups = new Map<string | null, RealizedRecord[]>();

  for (const record of records) {
    if (!isAccountInScope(record.account, accountScope)
      || !isDateInRange(record.closeDate, fromDate, toDate)) {
      continue;
    }
    const category = attributionCategory(record, groupBy);
    const group = groups.get(category) ?? [];
    group.push(record);
    groups.set(category, group);
  }

  const aggregates = [...groups.entries()].map(([category, groupedRecords]) => {
    const wins = groupedRecords.filter((record) => record.pnl.greaterThan(0)).length;
    const losses = groupedRecords.filter((record) => record.pnl.lessThan(0)).length;
    return {
      category,
      groupedRecords,
      wins,
      losses,
      netPnl: sumDecimals(groupedRecords.map((record) => record.pnl)),
      fees: sumDecimals(groupedRecords.map((record) => record.openFees.plus(record.closeFees))),
    };
  });
  const totalAbsolutePnl = sumDecimals(aggregates.map((aggregate) => aggregate.netPnl.abs()));

  const rows = aggregates
    .toSorted((left, right) => (
      right.netPnl.comparedTo(left.netPnl)
      || (left.category ?? "").localeCompare(right.category ?? "")
    ))
    .map((aggregate) => {
      const decisive = aggregate.wins + aggregate.losses;
      return {
        category: aggregate.category,
        closedLots: aggregate.groupedRecords.length,
        closeTransactions: new Set(
          aggregate.groupedRecords.map((record) => record.closeTransaction),
        ).size,
        wins: aggregate.wins,
        losses: aggregate.losses,
        winRatePct: decisive === 0
          ? null
          : new Decimal(aggregate.wins).mul(100).div(decisive)
            .toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN).toFixed(2),
        netPnl: aggregate.netPnl.toFixed(4),
        absoluteContributionPct: totalAbsolutePnl.equals(0)
          ? null
          : aggregate.netPnl.abs().mul(100).div(totalAbsolutePnl)
            .toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN).toFixed(2),
        fees: aggregate.fees.toFixed(6),
      };
    });

  return {
    rows,
    groups: rows.length,
    totalPnl: sumDecimals(aggregates.map((aggregate) => aggregate.netPnl)).toFixed(4),
    basis: "realized_fifo",
  };
}

/** 返回归因维度对应的字段值。 */
function attributionCategory(record: RealizedRecord, groupBy: AttributionGroupBy): string | null {
  switch (groupBy) {
    case "underlying":
      return record.underlying;
    case "asset-type":
      return record.assetType;
    case "direction":
      return record.direction;
    case "close-action":
      return record.closeAction;
  }
}

/** 合计 Decimal，空集合保持精确 Decimal 零。 */
function sumDecimals(values: readonly Decimal[]): Decimal {
  return values.reduce((total, value) => total.plus(value), new Decimal(0));
}

