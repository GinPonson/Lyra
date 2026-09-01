import { createHash } from "node:crypto";

import { Decimal } from "decimal.js";

import { IngestError } from "./errors.js";
import { parseDateField, parseMoney, parseOptionSymbol } from "./parsers.js";
import type {
  CashAction,
  ParsedTransaction,
  ParseTransactionContext,
  PositionAction,
  RawTransactionFields,
  TransactionAction,
} from "./types.js";

/** 美股标准期权一张合约对应 100 股。 */
const OPTION_MULTIPLIER = 100;

/** 与 Python 版本一致的金额勾稽最大误差，单位为美元。 */
const AMOUNT_TOLERANCE = new Decimal("0.011");

/** 买入类交易净额必须为负数，并包含费用支出。 */
const BUY_ACTIONS: ReadonlySet<TransactionAction> = new Set([
  "Buy",
  "Buy to Open",
  "Buy to Close",
]);

/** 参与 FIFO 重放的动作；集合也用于把未知字符串收窄为领域类型。 */
export const POSITION_ACTIONS: ReadonlySet<PositionAction> = new Set([
  "Buy",
  "Sell",
  "Buy to Open",
  "Sell to Open",
  "Buy to Close",
  "Sell to Close",
  "Expired",
  "Assigned",
]);

/** 只影响资金流水、不直接改变持仓的动作。 */
export const CASH_ACTIONS: ReadonlySet<CashAction> = new Set([
  "MoneyLink Transfer",
  "Credit Interest",
  "Qualified Dividend",
  "Non-Qualified Div",
  "Pr Yr Non-Qual Div",
  "NRA Tax Adj",
  "Pr Yr NRA Tax",
  "Foreign Tax Paid",
]);

/** 当前解析器明确接受的全部 Schwab 动作。 */
const KNOWN_ACTIONS: ReadonlySet<string> = new Set([...POSITION_ACTIONS, ...CASH_ACTIONS]);

/**
 * 把一行 Schwab 原始字段转换为标准交易。
 *
 * 该函数完成动作白名单、日期、资产类型、期权字段和金额勾稽。任一规则失败
 * 都抛出 `IngestError`，上层必须中止整个文件预览或提交，不能跳过当前行。
 *
 * @param fields - CSV 或 Excel 适配器提供的八个原始字段。
 * @param context - 账户、来源哈希、物理顺序和错误位置。
 * @returns occurrence 初始为 1 的标准交易；文件解析器可按重复次数重新编号。
 * @throws IngestError 当动作、字段、期权格式或成交金额不符合规则时抛出。
 */
export function parseTransactionRow(
  fields: RawTransactionFields,
  context: ParseTransactionContext,
): ParsedTransaction {
  const actionText = fields.Action.trim();
  if (!KNOWN_ACTIONS.has(actionText)) {
    throw new IngestError(
      `${context.where}: 未知 Action ${quote(actionText)},请扩展 KNOWN_ACTIONS 后再导入`,
    );
  }
  const action = actionText as TransactionAction;

  const { recordDate, transactionDate } = parseDateField(fields.Date);
  const rawSymbol = emptyToNull(fields.Symbol);
  const description = emptyToNull(fields.Description);
  const quantity = parseMoney(fields.Quantity);
  const price = parseMoney(fields.Price);
  const fees = parseMoney(fields["Fees & Comm"]);
  const amount = parseMoney(fields.Amount);

  let classification: Pick<
    ParsedTransaction,
    "assetType" | "underlying" | "expiry" | "strike" | "optionType"
  >;

  if (isCashAction(action)) {
    classification = {
      assetType: "cash",
      underlying: null,
      expiry: null,
      strike: null,
      optionType: null,
    };
  } else {
    if (quantity === null) {
      throw new IngestError(`${context.where}: 持仓类交易缺少数量 action=${quote(action)}`);
    }

    const option = parseOptionSymbol(rawSymbol ?? "");
    if (option !== null) {
      classification = {
        assetType: "option",
        underlying: option.underlying,
        expiry: option.expiry,
        strike: option.strike,
        optionType: option.optionType,
      };
    } else {
      const requiresOptionSymbol = action.includes(" to ") || action === "Expired" || action === "Assigned";
      if (requiresOptionSymbol) {
        throw new IngestError(
          `${context.where}: 期权 Symbol 格式无法解析: ${quote(rawSymbol)}`,
        );
      }
      classification = {
        assetType: "stock",
        underlying: rawSymbol,
        expiry: null,
        strike: null,
        optionType: null,
      };
    }

    // 到期和指派在现有 Schwab 导出中没有价格与金额，因此只校验数量和合约。
    if (action !== "Expired" && action !== "Assigned") {
      if (price === null || amount === null) {
        throw new IngestError(`${context.where}: 交易行缺少价格或金额 action=${quote(action)}`);
      }
      reconcileAmount({
        action,
        quantity,
        price,
        fees: fees ?? new Decimal(0),
        amount,
        multiplier: classification.assetType === "option" ? OPTION_MULTIPLIER : 1,
        where: context.where,
      });
    }
  }

  const businessHash = createBusinessHash({
    account: context.account,
    recordDate,
    transactionDate,
    action,
    rawSymbol,
    description,
    quantityText: decimalHashText(fields.Quantity),
    priceText: decimalHashText(fields.Price),
    feesText: decimalHashText(fields["Fees & Comm"]),
    amountText: decimalHashText(fields.Amount),
  });

  return {
    account: context.account,
    sourceType: context.sourceType,
    sourceReference: requireSourceReference(context.sourceReference, context.where),
    recordDate,
    transactionDate,
    action,
    rawSymbol,
    description,
    ...classification,
    quantity,
    price,
    fees,
    amount,
    sourceHash: context.sourceHash,
    sequence: context.sequence,
    occurrence: 1,
    businessHash,
    transactionHash: hashText(`${businessHash}|1`),
  };
}

/** 来源引用用于审计与查询，禁止空值进入标准交易。 */
function requireSourceReference(value: string, where: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new IngestError(`${where}: 来源引用不能为空`);
  }
  return normalized;
}

/**
 * 为同一文件内业务字段完全相同的真实交易设置稳定出现次数。
 *
 * @param transaction - 已标准化的交易。
 * @param occurrence - 从 1 开始的相同业务交易出现次数。
 * @returns 更新 occurrence 和交易哈希后的新对象，不修改原对象。
 * @throws RangeError 当 occurrence 不是正整数时抛出。
 */
export function withOccurrence(
  transaction: ParsedTransaction,
  occurrence: number,
): ParsedTransaction {
  if (!Number.isInteger(occurrence) || occurrence < 1) {
    throw new RangeError("occurrence 必须是从 1 开始的整数");
  }
  return {
    ...transaction,
    occurrence,
    transactionHash: hashText(`${transaction.businessHash}|${occurrence}`),
  };
}

/** 完成持仓交易净额与数量、价格、乘数、费用之间的勾稽。 */
function reconcileAmount(input: {
  action: PositionAction;
  quantity: Decimal;
  price: Decimal;
  fees: Decimal;
  amount: Decimal;
  multiplier: number;
  where: string;
}): void {
  const gross = input.quantity.mul(input.price).mul(input.multiplier);
  const expected = BUY_ACTIONS.has(input.action)
    ? gross.plus(input.fees).negated()
    : gross.minus(input.fees);

  if (input.amount.minus(expected).abs().greaterThan(AMOUNT_TOLERANCE)) {
    throw new IngestError(
      `${input.where}: 金额勾稽不符 action=${quote(input.action)} `
      + `qty=${input.quantity.toString()} price=${input.price.toString()} `
      + `fees=${input.fees.toString()} amount=${input.amount.toString()} `
      + `(期望 ${expected.toString()})`,
    );
  }
}

/** 判断动作是否属于现金流水，并在判断成功时收窄 TypeScript 类型。 */
function isCashAction(action: TransactionAction): action is CashAction {
  return CASH_ACTIONS.has(action as CashAction);
}

/** 去除字段两端空白，并把空文本统一为账本 `null`。 */
function emptyToNull(raw: string): string | null {
  const value = raw.trim();
  return value.length === 0 ? null : value;
}

/**
 * 生成与 Python `Decimal.__str__` 在 Schwab 固定小数格式下相同的哈希文本。
 *
 * Schwab CSV 使用普通十进制而非科学计数法。小数位数取自清洗后的源文本，
 * 这样 `$5.00` 不会被 Decimal.js 的 `toString()` 缩短为 `5`，保证迁移后的
 * 业务哈希与 Python 一致。科学计数法不是已确认的 Schwab 格式，直接使用
 * Decimal.js 规范文本；若真实样本出现该格式，应先补充跨语言 golden 测试。
 */
function decimalHashText(raw: string): string | null {
  const normalized = raw.trim().replaceAll("$", "").replaceAll(",", "");
  if (normalized.length === 0) {
    return null;
  }

  const value = new Decimal(normalized);
  if (/[eE]/.test(normalized)) {
    return value.toString();
  }
  const dotIndex = normalized.indexOf(".");
  const scale = dotIndex === -1 ? 0 : normalized.length - dotIndex - 1;
  return value.toFixed(scale);
}

/** 构造不依赖文件名和物理行号的业务指纹。 */
function createBusinessHash(input: {
  account: string;
  recordDate: string;
  transactionDate: string;
  action: TransactionAction;
  rawSymbol: string | null;
  description: string | null;
  quantityText: string | null;
  priceText: string | null;
  feesText: string | null;
  amountText: string | null;
}): string {
  const parts = [
    input.account,
    input.recordDate,
    input.transactionDate,
    input.action,
    hashValue(input.rawSymbol),
    hashValue(input.description),
    hashValue(input.quantityText),
    hashValue(input.priceText),
    hashValue(input.feesText),
    hashValue(input.amountText),
  ];
  return hashText(parts.join("|"));
}

/** 显式区分空值与数值或文本 `0`，保持 Python 哈希语义。 */
function hashValue(value: string | null): string {
  return value === null ? "<NULL>" : value;
}

/** 使用 SHA-256 生成小写十六进制文本。 */
function hashText(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

/** 生成接近 Python `repr` 的简洁字段显示，仅用于不含敏感信息的导入错误。 */
function quote(value: string | null): string {
  if (value === null) {
    return "None";
  }
  return `'${value.replaceAll("'", "\\'")}'`;
}
