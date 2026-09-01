/**
 * 交易导入规则无法接受输入时抛出的领域错误。
 *
 * 该错误只表示用户数据不符合已确认的 Schwab 格式。基础设施错误、数据库错误
 * 和程序缺陷不得包装成此错误，否则会把服务端故障误报为用户数据问题。
 */
export class IngestError extends Error {
  /**
   * 创建一个可安全返回给导入预览接口的领域错误。
   *
   * @param message - 不包含密钥、文件系统路径或堆栈信息的错误说明。
   */
  constructor(message: string) {
    super(message);
    this.name = "IngestError";
  }
}

