/** FIFO 重放发现账本交易不一致时抛出的领域错误。 */
export class FifoError extends Error {
  /**
   * @param message - 可定位到账户、日期、动作或合约的安全错误说明。
   */
  constructor(message: string) {
    super(message);
    this.name = "FifoError";
  }
}

