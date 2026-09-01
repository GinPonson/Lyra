import { Decimal } from "decimal.js";

import type { Lot, RealizedRecord } from "../fifo/index.js";
import {
  isDateInRange,
  MILLISECONDS_PER_DAY,
  parseIsoDate,
  validateDateRange,
} from "./dates.js";
import { aggregatePositions } from "./positions.js";
import { isAccountInScope } from "./scope.js";
import type {
  AccountScope,
  DrawdownResult,
  LongOptionUnderlyingRisk,
  OptionRiskBucket,
  OptionRiskResult,
} from "./types.js";

/** 期权风险桶的固定展示顺序。 */
const RISK_BUCKET_ORDER: readonly OptionRiskBucket[] = [
  "expired",
  "0-30d",
  "31-60d",
  "61-90d",
  "90d+",
];

/**
 * 按日计算累计已实现损益和峰谷回撤。
 *
 * @param records - FIFO 已实现明细。
 * @param accountScope - 显式账户范围。
 * @param fromDate - 可选起始平仓日期。
 * @param toDate - 可选截止平仓日期。
 * @returns 日度曲线、最大回撤峰谷和恢复日期。
 */
export function calculateDrawdown(
  records: readonly RealizedRecord[],
  accountScope: AccountScope,
  fromDate?: string,
  toDate?: string,
): DrawdownResult {
  validateDateRange(fromDate, toDate);
  const daily = new Map<string, Decimal>();
  for (const record of records) {
    if (!isAccountInScope(record.account, accountScope)
      || !isDateInRange(record.closeDate, fromDate, toDate)) {
      continue;
    }
    daily.set(record.closeDate, (daily.get(record.closeDate) ?? new Decimal(0)).plus(record.pnl));
  }

  let cumulative = new Decimal(0);
  let peak = new Decimal(0);
  let peakDate: string | null = null;
  let maxDrawdown = new Decimal(0);
  let maxPeakDate: string | null = null;
  let troughDate: string | null = null;
  let recoveryDate: string | null = null;
  let activePeak = new Decimal(0);
  const rows = [...daily.entries()]
    .toSorted(([left], [right]) => left.localeCompare(right))
    .map(([date, dailyPnl]) => {
      cumulative = cumulative.plus(dailyPnl);
      if (cumulative.greaterThan(peak)) {
        peak = cumulative;
        peakDate = date;
      }
      const currentDrawdown = peak.minus(cumulative);
      if (currentDrawdown.greaterThan(maxDrawdown)) {
        maxDrawdown = currentDrawdown;
        maxPeakDate = peakDate;
        troughDate = date;
        recoveryDate = null;
        activePeak = peak;
      } else if (
        troughDate !== null
        && recoveryDate === null
        && date > troughDate
        && cumulative.greaterThanOrEqualTo(activePeak)
      ) {
        recoveryDate = date;
      }
      return {
        date,
        dailyPnl: dailyPnl.toFixed(4),
        cumulativePnl: cumulative.toFixed(4),
        runningPeak: peak.toFixed(4),
        drawdown: currentDrawdown.toFixed(4),
      };
    });

  return {
    rows,
    days: rows.length,
    endingCumulativePnl: cumulative.toFixed(4),
    maxDrawdown: maxDrawdown.toFixed(4),
    peakDate: maxPeakDate,
    troughDate,
    recoveryDate,
    basis: "daily_realized_fifo",
  };
}

/**
 * 按到期窗口分别汇总当前长期权历史成本与短期权历史收入。
 *
 * 该结果只描述已支付或已收取的历史权利金，不使用市场价格，也不等同于完整
 * 风险价值。每个账户的 FIFO 批次先按持仓口径聚合，再进入风险桶。
 *
 * @param lots - FIFO 未平批次。
 * @param accountScope - 显式账户范围。
 * @param asOf - ISO 分析基准日。
 * @returns 风险桶、长期权集中度和历史权利金合计。
 */
export function calculateOptionRisk(
  lots: readonly Lot[],
  accountScope: AccountScope,
  asOf: string,
): OptionRiskResult {
  const analysisDate = parseIsoDate(asOf);
  const positions = aggregatePositions(lots, accountScope)
    .filter((position) => position.assetType === "option" && position.expiry !== null);
  const buckets = new Map<OptionRiskBucket, {
    positions: number;
    contracts: Decimal;
    longPositions: number;
    shortPositions: number;
    longContracts: Decimal;
    shortContracts: Decimal;
    longPremiumPaid: Decimal;
    shortPremiumReceived: Decimal;
  }>();
  const longByUnderlying = new Map<string | null, Decimal>();
  let totalLongPaid = new Decimal(0);
  let totalShortReceived = new Decimal(0);

  for (const position of positions) {
    const expiryDate = parseIsoDate(position.expiry as string);
    const daysRemaining = Math.round(
      (expiryDate.getTime() - analysisDate.getTime()) / MILLISECONDS_PER_DAY,
    );
    const bucket = riskBucket(daysRemaining);
    const premium = position.cost.abs().mul(100)
      .toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN);
    const item = buckets.get(bucket) ?? {
      positions: 0,
      contracts: new Decimal(0),
      longPositions: 0,
      shortPositions: 0,
      longContracts: new Decimal(0),
      shortContracts: new Decimal(0),
      longPremiumPaid: new Decimal(0),
      shortPremiumReceived: new Decimal(0),
    };
    item.positions += 1;
    item.contracts = item.contracts.plus(position.quantity);

    if (position.direction === "long") {
      item.longPositions += 1;
      item.longContracts = item.longContracts.plus(position.quantity);
      item.longPremiumPaid = item.longPremiumPaid.plus(premium);
      longByUnderlying.set(
        position.underlying,
        (longByUnderlying.get(position.underlying) ?? new Decimal(0)).plus(premium),
      );
      totalLongPaid = totalLongPaid.plus(premium);
    } else {
      item.shortPositions += 1;
      item.shortContracts = item.shortContracts.plus(position.quantity);
      item.shortPremiumReceived = item.shortPremiumReceived.plus(premium);
      totalShortReceived = totalShortReceived.plus(premium);
    }
    buckets.set(bucket, item);
  }

  const rows = RISK_BUCKET_ORDER
    .filter((bucket) => buckets.has(bucket))
    .map((bucket) => {
      const item = buckets.get(bucket);
      if (item === undefined) {
        throw new Error(`风险桶 ${bucket} 缺少聚合数据`);
      }
      return {
        bucket,
        positions: item.positions,
        contracts: item.contracts.toFixed(6),
        longPositions: item.longPositions,
        shortPositions: item.shortPositions,
        longContracts: item.longContracts.toFixed(6),
        shortContracts: item.shortContracts.toFixed(6),
        longPremiumPaid: item.longPremiumPaid.toFixed(2),
        shortPremiumReceived: item.shortPremiumReceived.toFixed(2),
        longPremiumPct: totalLongPaid.equals(0)
          ? null
          : item.longPremiumPaid.mul(100).div(totalLongPaid)
            .toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN).toFixed(2),
      };
    });
  const ranked = [...longByUnderlying.entries()].toSorted((left, right) => (
    right[1].comparedTo(left[1]) || (left[0] ?? "").localeCompare(right[0] ?? "")
  ));
  const topThree = ranked.slice(0, 3);
  const topThreeCost = topThree.reduce(
    (total, [, cost]) => total.plus(cost),
    new Decimal(0),
  );
  const topThreeLongUnderlyings: LongOptionUnderlyingRisk[] = topThree.map(([
    underlying,
    cost,
  ]) => ({
    underlying,
    longPremiumPaid: cost.toFixed(2),
    longPremiumPct: totalLongPaid.equals(0)
      ? null
      : cost.mul(100).div(totalLongPaid)
        .toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN).toFixed(2),
  }));

  return {
    rows,
    asOf,
    optionPositions: positions.length,
    totalLongPremiumPaid: totalLongPaid.toFixed(2),
    totalShortPremiumReceived: totalShortReceived.toFixed(2),
    topThreeLongUnderlyings,
    topThreeLongConcentrationPct: totalLongPaid.equals(0)
      ? null
      : topThreeCost.mul(100).div(totalLongPaid)
        .toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN).toFixed(2),
    basis: "long_paid_and_short_received_premium_not_market_risk",
  };
}

/** 把相对到期天数映射到稳定风险桶。 */
function riskBucket(daysRemaining: number): OptionRiskBucket {
  if (daysRemaining < 0) {
    return "expired";
  }
  if (daysRemaining <= 30) {
    return "0-30d";
  }
  if (daysRemaining <= 60) {
    return "31-60d";
  }
  if (daysRemaining <= 90) {
    return "61-90d";
  }
  return "90d+";
}

