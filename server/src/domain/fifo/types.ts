import type { Decimal } from "decimal.js";

import type {
  LocalDate,
  ParsedTransaction,
  PositionAction,
} from "../ledger/index.js";

/** FIFO 批次的持仓方向。 */
export type LotDirection = "long" | "short";

/** 一个尚未完全平仓的 FIFO 批次。 */
export interface Lot {
  account: string;
  symbolKey: string | null;
  assetType: "stock" | "option";
  underlying: string | null;
  expiry: LocalDate | null;
  strike: Decimal | null;
  optionType: "C" | "P" | null;
  direction: LotDirection;
  openDate: LocalDate;
  openPrice: Decimal;
  openQuantity: Decimal;
  remainingQuantity: Decimal;
  openFees: Decimal;
  openTransaction: string;
  /** 已进入 realized 的开仓费用，只用于保证分摊尾差对齐。 */
  feesAllocated: Decimal;
}

/** 一次平仓对某个开仓批次形成的已实现结果。 */
export interface RealizedRecord {
  account: string;
  symbolKey: string | null;
  assetType: "stock" | "option";
  underlying: string | null;
  direction: LotDirection;
  openDate: LocalDate;
  closeDate: LocalDate;
  quantity: Decimal;
  openPrice: Decimal;
  closePrice: Decimal;
  openFees: Decimal;
  closeFees: Decimal;
  pnl: Decimal;
  closeAction: PositionAction;
  openTransaction: string;
  closeTransaction: string;
}

/** 纯领域 FIFO 重放的完整结果；数据库层负责在事务中原子替换旧产物。 */
export interface FifoReplayResult {
  transactionsReplayed: number;
  openLots: readonly Lot[];
  realizedRecords: readonly RealizedRecord[];
  totalRealizedPnl: string;
  warnings: readonly string[];
}

/** FIFO 内核当前接受的输入，包含现金行以确定全账本最大日期。 */
export type FifoTransaction = ParsedTransaction;

