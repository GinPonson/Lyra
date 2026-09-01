/**
 * 不含时区的账本日期，固定使用 ISO `YYYY-MM-DD` 形式。
 *
 * 交易日期属于券商账本的日历日期，不能使用本地时区的 JavaScript `Date`
 * 表示，否则在服务器时区变化时可能出现前后偏移一天的问题。
 */
export type LocalDate = string;

/** 标准交易允许的权威来源；值与 DuckDB `transactions.source_type` 一致。 */
export type LedgerSourceType = "csv" | "excel" | "gmail";

/** Schwab Date 字段解析后的记录日期与生效日期。 */
export interface ParsedDateField {
  /** Schwab 导出行首先展示的记录日期。 */
  recordDate: LocalDate;
  /** `as of` 后的实际生效日期；普通日期与记录日期相同。 */
  transactionDate: LocalDate;
}

/** Schwab 文本期权代码中可确定的合约字段。 */
export interface ParsedOptionSymbol {
  /** 标的代码，例如 `GOOG` 或 `BRK.B`。 */
  underlying: string;
  /** 到期日，固定为 ISO `YYYY-MM-DD`。 */
  expiry: LocalDate;
  /** 行权价；金额计算始终使用 Decimal。 */
  strike: import("decimal.js").Decimal;
  /** `C` 表示看涨期权，`P` 表示看跌期权。 */
  optionType: "C" | "P";
}

/** Schwab 当前已确认的持仓类动作。 */
export type PositionAction =
  | "Buy"
  | "Sell"
  | "Buy to Open"
  | "Sell to Open"
  | "Buy to Close"
  | "Sell to Close"
  | "Expired"
  | "Assigned";

/** Schwab 当前已确认的现金类动作。 */
export type CashAction =
  | "MoneyLink Transfer"
  | "Credit Interest"
  | "Qualified Dividend"
  | "Non-Qualified Div"
  | "Pr Yr Non-Qual Div"
  | "NRA Tax Adj"
  | "Pr Yr NRA Tax"
  | "Foreign Tax Paid";

/** 标准交易允许的动作全集；未知动作必须阻断导入。 */
export type TransactionAction = PositionAction | CashAction;

/** CSV 或 Excel 适配器提交给标准化器的八个 Schwab 原始字段。 */
export interface RawTransactionFields {
  Date: string;
  Action: string;
  Symbol: string;
  Description: string;
  Quantity: string;
  Price: string;
  "Fees & Comm": string;
  Amount: string;
}

/** 已完成字段校验、资产分类、金额勾稽和哈希的标准交易。 */
export interface ParsedTransaction {
  account: string;
  sourceType: LedgerSourceType;
  /** 导入作业或 Gmail message ID 等可审计来源引用。 */
  sourceReference: string;
  recordDate: LocalDate;
  transactionDate: LocalDate;
  action: TransactionAction;
  rawSymbol: string | null;
  description: string | null;
  assetType: "stock" | "option" | "cash";
  underlying: string | null;
  expiry: LocalDate | null;
  strike: import("decimal.js").Decimal | null;
  optionType: "C" | "P" | null;
  quantity: import("decimal.js").Decimal | null;
  price: import("decimal.js").Decimal | null;
  fees: import("decimal.js").Decimal | null;
  amount: import("decimal.js").Decimal | null;
  sourceHash: string;
  sequence: number;
  occurrence: number;
  businessHash: string;
  transactionHash: string;
}

/** 标准化单行交易时由文件适配器提供的上下文。 */
export interface ParseTransactionContext {
  /** 从文件名解析并经用户确认的账户标识。 */
  account: string;
  /** 原始文件内容哈希，用于导入审计。 */
  sourceHash: string;
  sourceType: LedgerSourceType;
  sourceReference: string;
  /** 从 1 开始的数据行顺序。 */
  sequence: number;
  /** 面向用户的错误位置，例如 `file.csv 第2行`。 */
  where: string;
}

/** 文件适配器完成格式解析后，提交给整批标准化器的输入。 */
export interface TransactionBatchInput {
  /** 仅用于错误定位和预览展示的原始文件名。 */
  fileName: string;
  /** 已从文件名解析的账户标识。 */
  account: string;
  /** 原始文件 SHA-256。 */
  sourceHash: string;
  /** 来源必须由导入或 Gmail 应用层显式给出，标准化器不根据文件名猜测。 */
  sourceType: LedgerSourceType;
  sourceReference: string;
  /** 按文件物理顺序排列的 Schwab 原始交易行。 */
  rows: readonly RawTransactionFields[];
}

/** 整个文件成功通过领域校验后产生的预览核心结果。 */
export interface TransactionBatchPreview {
  fileName: string;
  account: string;
  sourceHash: string;
  totalRows: number;
  /** 每种动作在文件内出现的次数。 */
  byAction: Readonly<Record<string, number>>;
  /** 按文件顺序排列并完成 occurrence 编号的标准交易。 */
  transactions: readonly ParsedTransaction[];
}
