import { Decimal } from "decimal.js";

import { FifoError } from "./errors.js";
import type {
  FifoReplayResult,
  FifoTransaction,
  Lot,
  LotDirection,
  RealizedRecord,
} from "./types.js";
import type { PositionAction } from "../ledger/index.js";

/** 开仓动作到持仓方向的映射，与现有 Python FIFO 保持一致。 */
const OPEN_DIRECTIONS: Readonly<Partial<Record<PositionAction, LotDirection>>> = {
  Buy: "long",
  "Buy to Open": "long",
  "Sell to Open": "short",
};

/** 平仓动作到被平持仓方向的映射；Expired 需要从现有批次判断方向。 */
const CLOSE_DIRECTIONS: Readonly<Partial<Record<PositionAction, LotDirection>>> = {
  Sell: "long",
  "Sell to Close": "long",
  "Buy to Close": "short",
  Assigned: "short",
};

/**
 * 按 Schwab 语义全量重放标准交易，生成未平批次和已实现损益。
 *
 * 输入不会被修改，因此对同一交易集合重复调用会得到同一结果。函数先验证
 * 同账户、同日、同合约是否混入未对齐文件，再按生效日升序、同日物理顺序
 * 降序、交易哈希升序重放。任何超卖或方向错误立即抛出，不返回部分结果。
 *
 * @param transactions - 已完成标准化和 occurrence 编号的全部账本交易。
 * @returns 可由数据库层原子持久化的 FIFO 结果。
 * @throws FifoError 当顺序不可证明、超卖、无持仓可平或动作不受支持时抛出。
 */
export function replayFifo(transactions: readonly FifoTransaction[]): FifoReplayResult {
  assertReplayOrderIsProvable(transactions);

  const positionTransactions = transactions
    .filter((transaction) => transaction.assetType === "stock" || transaction.assetType === "option")
    .toSorted(compareReplayOrder);
  const books = new Map<string, Lot[]>();
  const realizedRecords: RealizedRecord[] = [];

  for (const transaction of positionTransactions) {
    const directionToOpen = OPEN_DIRECTIONS[transaction.action as PositionAction];
    if (directionToOpen !== undefined) {
      const key = bookKey(transaction.account, transaction.rawSymbol);
      const book = books.get(key) ?? [];
      book.push(openLot(transaction, directionToOpen));
      books.set(key, book);
      continue;
    }

    const directionToClose = CLOSE_DIRECTIONS[transaction.action as PositionAction];
    if (directionToClose !== undefined) {
      const closePrice = transaction.price ?? new Decimal(0);
      closePosition(transaction, directionToClose, closePrice, books, realizedRecords);
      continue;
    }

    if (transaction.action === "Expired") {
      const book = books.get(bookKey(transaction.account, transaction.rawSymbol)) ?? [];
      const directions = new Set(
        book
          .filter((lot) => lot.remainingQuantity.greaterThan(0))
          .map((lot) => lot.direction),
      );
      if (directions.size !== 1) {
        const current = directions.size === 0 ? "无持仓" : [...directions].join(",");
        throw new FifoError(
          `${transaction.transactionDate} Expired ${displaySymbol(transaction.rawSymbol)}: `
          + `无法确定持仓方向(现有方向: ${current})`,
        );
      }
      const [direction] = directions;
      if (direction === undefined) {
        throw new FifoError("Expired 持仓方向缺失");
      }
      closePosition(transaction, direction, new Decimal(0), books, realizedRecords);
      continue;
    }

    throw new FifoError(`未处理的持仓 Action: '${transaction.action}'`);
  }

  const openLots = [...books.values()]
    .flat()
    .filter((lot) => lot.remainingQuantity.greaterThan(0));
  const maxTransactionDate = transactions
    .map((transaction) => transaction.transactionDate)
    .toSorted()
    .at(-1);
  const warnings = maxTransactionDate === undefined
    ? []
    : openLots
      .filter((lot) => (
        lot.assetType === "option"
        && lot.expiry !== null
        && lot.expiry < maxTransactionDate
      ))
      .map((lot) => (
        `期权 ${displaySymbol(lot.symbolKey)} 已于 ${lot.expiry} 到期,`
        + `仍有 ${lot.remainingQuantity.toString()} 张未平`
      ));

  const totalRealizedPnl = realizedRecords
    .reduce((total, record) => total.plus(record.pnl), new Decimal(0))
    .toDecimalPlaces(4, Decimal.ROUND_HALF_EVEN)
    .toFixed(4);

  return {
    transactionsReplayed: positionTransactions.length,
    openLots,
    realizedRecords,
    totalRealizedPnl,
    warnings,
  };
}

/** 创建开仓批次，并验证标准化器承诺的必填数值仍然存在。 */
function openLot(transaction: FifoTransaction, direction: LotDirection): Lot {
  if (transaction.quantity === null || transaction.price === null) {
    throw new FifoError(
      `${transaction.transactionDate} ${transaction.action} ${displaySymbol(transaction.rawSymbol)}: `
      + "开仓交易缺少数量或价格",
    );
  }
  if (transaction.assetType !== "stock" && transaction.assetType !== "option") {
    throw new FifoError(`开仓交易资产类型无效: ${transaction.assetType}`);
  }

  return {
    account: transaction.account,
    symbolKey: transaction.rawSymbol,
    assetType: transaction.assetType,
    underlying: transaction.underlying,
    expiry: transaction.expiry,
    strike: transaction.strike,
    optionType: transaction.optionType,
    direction,
    openDate: transaction.transactionDate,
    openPrice: transaction.price,
    openQuantity: transaction.quantity,
    remainingQuantity: transaction.quantity,
    openFees: transaction.fees ?? new Decimal(0),
    openTransaction: transaction.transactionHash,
    feesAllocated: new Decimal(0),
  };
}

/** 按 FIFO 消耗指定方向的批次，并为每段消耗生成 realized 记录。 */
function closePosition(
  transaction: FifoTransaction,
  direction: LotDirection,
  closePrice: Decimal,
  books: Map<string, Lot[]>,
  realizedRecords: RealizedRecord[],
): void {
  if (transaction.quantity === null) {
    throw new FifoError(
      `${transaction.transactionDate} ${transaction.action} ${displaySymbol(transaction.rawSymbol)}: `
      + "平仓交易缺少数量",
    );
  }
  const key = bookKey(transaction.account, transaction.rawSymbol);
  const book = books.get(key) ?? [];
  const where = `${transaction.transactionDate} ${transaction.action} ${displaySymbol(transaction.rawSymbol)}`;
  if (book.length === 0 || !book.some((lot) => lot.direction === direction)) {
    throw new FifoError(`${where}: 没有可平的 ${direction} 持仓`);
  }

  const consumed = consume(book, transaction.quantity, where);
  const closeFeesTotal = transaction.fees ?? new Decimal(0);
  let closeFeesAllocated = new Decimal(0);
  const multiplier = transaction.assetType === "option" ? 100 : 1;

  consumed.forEach(({ lot, quantity }, index) => {
    const openFees = allocateOpenFee(lot, quantity);
    const isLast = index === consumed.length - 1;
    const closeFees = isLast
      ? closeFeesTotal.minus(closeFeesAllocated)
      : closeFeesTotal.mul(quantity).div(transaction.quantity as Decimal);
    if (!isLast) {
      closeFeesAllocated = closeFeesAllocated.plus(closeFees);
    }

    const gross = lot.direction === "long"
      ? closePrice.minus(lot.openPrice).mul(quantity).mul(multiplier)
      : lot.openPrice.minus(closePrice).mul(quantity).mul(multiplier);
    const pnl = gross
      .minus(openFees)
      .minus(closeFees)
      .toDecimalPlaces(4, Decimal.ROUND_HALF_EVEN);

    realizedRecords.push({
      account: lot.account,
      symbolKey: lot.symbolKey,
      assetType: lot.assetType,
      underlying: lot.underlying,
      direction: lot.direction,
      openDate: lot.openDate,
      closeDate: transaction.transactionDate,
      quantity,
      openPrice: lot.openPrice,
      closePrice,
      openFees,
      closeFees,
      pnl,
      closeAction: transaction.action as PositionAction,
      openTransaction: lot.openTransaction,
      closeTransaction: transaction.transactionHash,
    });
  });

  books.set(key, book);
}

/** 从一个合约的 FIFO 队列消耗数量；队列不足时不继续计算。 */
function consume(
  book: Lot[],
  requestedQuantity: Decimal,
  where: string,
): Array<{ lot: Lot; quantity: Decimal }> {
  const available = book.reduce(
    (total, lot) => total.plus(lot.remainingQuantity),
    new Decimal(0),
  );
  if (available.lessThan(requestedQuantity)) {
    throw new FifoError(
      `${where}: 平仓数量 ${requestedQuantity.toString()} 超过未平数量 ${available.toString()}`
      + `(合约 ${displaySymbol(book[0]?.symbolKey ?? null)},方向 ${book[0]?.direction ?? "?"})`,
    );
  }

  const consumed: Array<{ lot: Lot; quantity: Decimal }> = [];
  let left = requestedQuantity;
  while (left.greaterThan(0)) {
    const lot = book[0];
    if (lot === undefined) {
      throw new FifoError(`${where}: FIFO 队列意外为空`);
    }
    const quantity = Decimal.min(lot.remainingQuantity, left);
    lot.remainingQuantity = lot.remainingQuantity.minus(quantity);
    left = left.minus(quantity);
    consumed.push({ lot, quantity });
    if (lot.remainingQuantity.equals(0)) {
      book.shift();
    }
  }
  return consumed;
}

/** 按消耗比例分摊开仓费；批次耗尽时一次性分配剩余尾差。 */
function allocateOpenFee(lot: Lot, quantity: Decimal): Decimal {
  const allocated = lot.remainingQuantity.equals(0)
    ? lot.openFees.minus(lot.feesAllocated)
    : lot.openFees.mul(quantity).div(lot.openQuantity);
  lot.feesAllocated = lot.feesAllocated.plus(allocated);
  return allocated;
}

/** 检查同账户、同日、同合约的顺序是否来自同一可比较文件快照。 */
function assertReplayOrderIsProvable(transactions: readonly FifoTransaction[]): void {
  const groups = new Map<string, {
    account: string;
    transactionDate: string;
    symbol: string | null;
    sources: Set<string>;
  }>();

  for (const transaction of transactions) {
    if (transaction.assetType !== "stock" && transaction.assetType !== "option") {
      continue;
    }
    const key = JSON.stringify([
      transaction.account,
      transaction.transactionDate,
      transaction.rawSymbol,
    ]);
    const group = groups.get(key) ?? {
      account: transaction.account,
      transactionDate: transaction.transactionDate,
      symbol: transaction.rawSymbol,
      sources: new Set<string>(),
    };
    group.sources.add(transaction.sourceHash);
    groups.set(key, group);
  }

  const ambiguous = [...groups.values()]
    .filter((group) => group.sources.size > 1)
    .toSorted((left, right) => (
      left.account.localeCompare(right.account)
      || left.transactionDate.localeCompare(right.transactionDate)
      || displaySymbol(left.symbol).localeCompare(displaySymbol(right.symbol))
    ))[0];
  if (ambiguous !== undefined) {
    throw new FifoError(
      `${ambiguous.transactionDate} ${displaySymbol(ambiguous.symbol)}: `
      + `同日交易来自 ${ambiguous.sources.size} 个未对齐的 CSV 快照`
      + `(账户 ${ambiguous.account})，无法可靠确定 FIFO 顺序；`
      + "请导入覆盖该日全部交易的最新导出文件",
    );
  }
}

/** Python FIFO 的稳定重放顺序：日期升序、同日 seq 降序、哈希升序。 */
function compareReplayOrder(left: FifoTransaction, right: FifoTransaction): number {
  return left.transactionDate.localeCompare(right.transactionDate)
    || right.sequence - left.sequence
    || left.transactionHash.localeCompare(right.transactionHash);
}

/** 使用 JSON 元组避免账户和合约文本拼接产生键冲突。 */
function bookKey(account: string, symbol: string | null): string {
  return JSON.stringify([account, symbol]);
}

/** 仅用于安全错误和告警显示，不改变账本中的原始值。 */
function displaySymbol(symbol: string | null): string {
  return symbol ?? "未知";
}

