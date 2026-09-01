import { Decimal } from "decimal.js";

import type { Lot } from "../fifo/index.js";
import { formatIsoDate, MILLISECONDS_PER_DAY, parseIsoDate } from "./dates.js";
import { isAccountInScope } from "./scope.js";
import type {
  AccountScope,
  ExpiringPositionRow,
  ExpiringPositionsResult,
  PositionRow,
} from "./types.js";

/**
 * 按账户、完整合约和方向聚合当前未平 FIFO 批次。
 *
 * @param lots - FIFO 重放得到的未平批次。
 * @param accountScope - 必填账户范围；`all` 仍保留每个账户的独立聚合行。
 * @param underlying - 可选标的过滤，调用方应传入用户期望的代码。
 * @returns 与 Python `v_positions` 同口径的稳定排序结果。
 */
export function aggregatePositions(
  lots: readonly Lot[],
  accountScope: AccountScope,
  underlying?: string,
): readonly PositionRow[] {
  const normalizedUnderlying = underlying?.toUpperCase();
  const groups = new Map<string, PositionRow>();

  for (const lot of lots) {
    if (!lot.remainingQuantity.greaterThan(0) || !isAccountInScope(lot.account, accountScope)) {
      continue;
    }
    if (normalizedUnderlying !== undefined && lot.underlying !== normalizedUnderlying) {
      continue;
    }

    const key = JSON.stringify([
      lot.account,
      lot.symbolKey,
      lot.assetType,
      lot.underlying,
      lot.expiry,
      lot.strike?.toString() ?? null,
      lot.optionType,
      lot.direction,
    ]);
    const existing = groups.get(key);
    if (existing === undefined) {
      groups.set(key, {
        account: lot.account,
        symbolKey: lot.symbolKey,
        assetType: lot.assetType,
        underlying: lot.underlying,
        expiry: lot.expiry,
        strike: lot.strike,
        optionType: lot.optionType,
        direction: lot.direction,
        quantity: lot.remainingQuantity,
        cost: lot.remainingQuantity.mul(lot.openPrice),
        firstOpenDate: lot.openDate,
      });
      continue;
    }

    existing.quantity = existing.quantity.plus(lot.remainingQuantity);
    existing.cost = existing.cost.plus(lot.remainingQuantity.mul(lot.openPrice));
    if (lot.openDate < existing.firstOpenDate) {
      existing.firstOpenDate = lot.openDate;
    }
  }

  return [...groups.values()].toSorted(comparePositions);
}

/**
 * 选出截止窗口内的未平期权并标记已过期、今日到期或即将到期。
 *
 * @param positions - 已按 `aggregatePositions` 聚合的持仓。
 * @param asOf - ISO 基准日，必须是实际存在的日历日期。
 * @param days - 向未来观察的天数，不能为负数。
 * @returns 到期明细、截止日期和各状态计数。
 * @throws RangeError 当日期无效或天数为负时抛出。
 */
export function analyzeExpiringPositions(
  positions: readonly PositionRow[],
  asOf: string,
  days: number,
): ExpiringPositionsResult {
  const analysisDate = parseIsoDate(asOf);
  if (!Number.isInteger(days) || days < 0) {
    throw new RangeError("days 必须是非负整数");
  }
  const cutoffDate = new Date(analysisDate.getTime());
  cutoffDate.setUTCDate(cutoffDate.getUTCDate() + days);
  const cutoff = formatIsoDate(cutoffDate);

  const rows: ExpiringPositionRow[] = positions
    .filter((position) => (
      position.assetType === "option"
      && position.expiry !== null
      && position.expiry <= cutoff
    ))
    .map((position) => {
      const expiryDate = parseIsoDate(position.expiry as string);
      const daysRemaining = Math.round(
        (expiryDate.getTime() - analysisDate.getTime()) / MILLISECONDS_PER_DAY,
      );
      const status: ExpiringPositionRow["status"] = daysRemaining < 0
        ? "expired"
        : daysRemaining === 0
          ? "expires_today"
          : "upcoming";
      return {
        ...position,
        status,
        daysRemaining,
        premiumDollars: position.cost
          .mul(100)
          .toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN),
      };
    })
    .toSorted((left, right) => (
      (left.expiry ?? "").localeCompare(right.expiry ?? "")
      || (left.underlying ?? "").localeCompare(right.underlying ?? "")
      || compareNullableDecimal(left.strike, right.strike)
      || left.direction.localeCompare(right.direction)
    ));

  return {
    rows,
    count: rows.length,
    asOf,
    cutoff,
    expired: rows.filter((row) => row.status === "expired").length,
    expiresToday: rows.filter((row) => row.status === "expires_today").length,
    upcoming: rows.filter((row) => row.status === "upcoming").length,
  };
}

/** 保持 Python 查询的资产、标的、到期日顺序，并补充账户作为稳定最终键。 */
function comparePositions(left: PositionRow, right: PositionRow): number {
  return left.assetType.localeCompare(right.assetType)
    || (left.underlying ?? "").localeCompare(right.underlying ?? "")
    || (left.expiry ?? "").localeCompare(right.expiry ?? "")
    || left.account.localeCompare(right.account)
    || (left.symbolKey ?? "").localeCompare(right.symbolKey ?? "")
    || left.direction.localeCompare(right.direction);
}

/** 比较允许为空的行权价。 */
function compareNullableDecimal(left: Decimal | null, right: Decimal | null): number {
  if (left === null && right === null) {
    return 0;
  }
  if (left === null) {
    return -1;
  }
  if (right === null) {
    return 1;
  }
  return left.comparedTo(right);
}
