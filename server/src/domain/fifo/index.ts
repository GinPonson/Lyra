/** FIFO 领域模块公共导出。 */
export { FifoError } from "./errors.js";
export { replayFifo } from "./replay.js";
export type {
  FifoReplayResult,
  FifoTransaction,
  Lot,
  LotDirection,
  RealizedRecord,
} from "./types.js";

