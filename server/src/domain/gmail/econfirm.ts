import { Decimal } from "decimal.js";

import { IngestError, parseOptionSymbol } from "../ledger/index.js";
import type {
  LocalDate,
  PositionAction,
  RawTransactionFields,
} from "../ledger/index.js";

/** 已由迁移样本确认的 Schwab eConfirm 发件地址。 */
export const SCHWAB_ECONFIRM_SENDER = "donotreply@mail.schwab.com";

/** 已由迁移样本确认的 eConfirm Subject 固定片段。 */
export const SCHWAB_ECONFIRM_SUBJECT = "Schwab eConfirms";

/** 只接受已确认发件地址，不因同域名而信任其他邮箱。 */
export const SCHWAB_SENDER_PATTERN = /(?:^|[<\s])donotreply@mail\.schwab\.com(?:>|\s|$)/i;

/** eConfirm 中账户尾号的两种已确认文本形式。 */
const ACCOUNT_SUFFIX_PATTERN = /Account\s+ending(?:\s+in\s+|\s*:\s*)([A-Za-z0-9]+)/gi;

/** 每个交易块以独立的 Symbol 标签开始。 */
const SYMBOL_SPLIT_PATTERN = /(?:^|\n)\s*Symbol:\s*\n/i;

/** eConfirm 金额字段必须显式带美元符号。 */
const MONEY_PATTERN = /^-?\$[\d,]+(?:\.\d+)?$/;

/** 期权成交方向与成交后持仓类型共同决定 Schwab 标准动作。 */
const OPTION_ACTIONS = new Map<string, PositionAction>([
  ["Purchase|Margin", "Buy to Open"],
  ["Sale|Margin", "Sell to Close"],
  ["Sale|Short", "Sell to Open"],
  ["Purchase|Short", "Buy to Close"],
]);

/** 股票不区分 Open/Close，仅由买卖方向决定动作。 */
const STOCK_ACTIONS = new Map<string, PositionAction>([
  ["Purchase", "Buy"],
  ["Sale", "Sell"],
]);

/** Gmail 或 MIME 适配器完成文本提取后的标准邮件。 */
export interface GmailMessage {
  messageId: string;
  sender: string;
  subject: string;
  body: string;
  emailTimestamp: string | null;
  bodyCandidates?: readonly string[];
}

/** 从 Schwab eConfirm 确定性提取的一笔成交。 */
export interface EmailTrade {
  tradeDate: LocalDate;
  action: PositionAction;
  symbol: string;
  description: string;
  quantity: Decimal;
  price: Decimal;
  fees: Decimal;
  amount: Decimal;
}

/**
 * 从 Subject 和全部正文候选提取唯一账户尾号。
 *
 * @throws IngestError 当没有尾号或不同候选给出冲突尾号时抛出。
 */
export function extractAccountSuffix(message: GmailMessage): string {
  const suffixes = new Set<string>();
  for (const source of [message.subject, ...candidateBodies(message)]) {
    for (const match of source.matchAll(ACCOUNT_SUFFIX_PATTERN)) {
      const suffix = match[1];
      if (suffix !== undefined) {
        suffixes.add(suffix);
      }
    }
  }
  if (suffixes.size === 0) {
    throw new IngestError(`邮件 ${message.messageId}: 缺少 Account ending`);
  }
  if (suffixes.size !== 1) {
    throw new IngestError(
      `邮件 ${message.messageId}: Subject 与正文中的账户尾号不一致: `
      + `${JSON.stringify([...suffixes].toSorted())}`,
    );
  }
  const [suffix] = suffixes;
  if (suffix === undefined) {
    throw new IngestError(`邮件 ${message.messageId}: 账户尾号缺失`);
  }
  return suffix;
}

/**
 * 严格解析邮件的全部正文候选，并拒绝候选之间不一致的交易结果。
 *
 * 一个 multipart 邮件可以同时提供 plain 和 HTML 转换文本。只要存在一个可
 * 解析候选即可继续，但多个成功候选必须生成完全相同的交易集合。
 *
 * @param message - 已由 Gmail/MIME 层提取头部和正文的标准邮件。
 * @returns 邮件内按展示顺序排列的成交。
 */
export function parseEconfirm(message: GmailMessage): readonly EmailTrade[] {
  const successes = new Map<string, readonly EmailTrade[]>();
  const errors: string[] = [];

  candidateBodies(message).forEach((body, index) => {
    try {
      const trades = parseEconfirmBody(message.messageId, body);
      successes.set(tradesFingerprint(trades), trades);
    } catch (error: unknown) {
      if (error instanceof IngestError) {
        errors.push(`候选${index + 1}: ${error.message}`);
        return;
      }
      throw error;
    }
  });

  if (successes.size === 0) {
    throw new IngestError(
      `邮件 ${message.messageId}: 所有正文候选解析失败: ${errors.join("；")}`,
    );
  }
  if (successes.size !== 1) {
    throw new IngestError(`邮件 ${message.messageId}: plain 与 HTML 解析出的交易不一致`);
  }
  const [trades] = successes.values();
  if (trades === undefined) {
    throw new IngestError(`邮件 ${message.messageId}: 成交解析结果缺失`);
  }
  return trades;
}

/**
 * 把邮件成交转换为与 Schwab Transactions 文件一致的八个原始字段。
 *
 * 该桥接让 Gmail 和 Excel/CSV 共用同一交易标准化、金额勾稽和哈希逻辑，
 * 不在邮件链路中建立第二套账本模型。
 *
 * @param trade - 已通过 eConfirm Principal 与 Total Amount 勾稽的成交。
 * @returns 可直接交给 `normalizeTransactionBatch` 的 Schwab 八列字段。
 */
export function emailTradeToRawFields(trade: EmailTrade): RawTransactionFields {
  const [year, month, day] = trade.tradeDate.split("-");
  if (year === undefined || month === undefined || day === undefined) {
    throw new IngestError(`邮件成交日期无法格式化: ${trade.tradeDate}`);
  }
  return {
    Date: `${month}/${day}/${year}`,
    Action: trade.action,
    Symbol: trade.symbol,
    Description: trade.description,
    Quantity: plainDecimal(trade.quantity),
    Price: `$${plainDecimal(trade.price)}`,
    "Fees & Comm": trade.fees.equals(0) ? "" : `$${plainDecimal(trade.fees)}`,
    Amount: signedUsd(trade.amount),
  };
}

/** 把单个正文候选拆成交易块并逐笔解析。 */
function parseEconfirmBody(messageId: string, body: string): readonly EmailTrade[] {
  const parts = body.split(SYMBOL_SPLIT_PATTERN);
  if (parts.length < 2) {
    throw new IngestError("正文中没有交易块");
  }

  const trades: EmailTrade[] = [];
  parts.slice(1).forEach((rawBlock, index) => {
    const block = rawBlock.split("Additional information for this security:", 1)[0] ?? "";
    const where = `邮件 ${messageId} 第${index + 1}笔`;
    const firstLine = block.split(/\r?\n/).map((line) => line.trim()).find(Boolean) ?? "";
    const symbol = unwrapSymbol(firstLine);
    const securityDescription = field(block, "Security Description", where);
    const reportedAction = field(block, "Action", where);
    const positionType = field(block, "Type", where);
    const tradeDate = parseTradeDate(field(block, "Trade Date", where), where);

    const option = parseOptionSymbol(symbol);
    const action = option === null
      ? STOCK_ACTIONS.get(reportedAction)
      : OPTION_ACTIONS.get(`${reportedAction}|${positionType}`);
    if (action === undefined) {
      throw new IngestError(
        `${where}: 无法映射 Action/Type: '${reportedAction}'/'${positionType}'`,
      );
    }

    const marker = /Total Amount\s*\n/i.exec(block);
    if (marker === null || marker.index === undefined) {
      throw new IngestError(`${where}: 缺少成交金额表`);
    }
    const values = block.slice(marker.index + marker[0].length)
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
    trades.push(...parseValueRows({
      values,
      where,
      tradeDate,
      action,
      symbol,
      description: normalizeDescription(symbol, securityDescription),
      multiplier: option === null ? 1 : 100,
    }));
  });
  return trades;
}

/** 解析金额表，并在费用为零且结构明确时展开多价格成交。 */
function parseValueRows(input: {
  values: readonly string[];
  where: string;
  tradeDate: LocalDate;
  action: PositionAction;
  symbol: string;
  description: string;
  multiplier: number;
}): readonly EmailTrade[] {
  let values = [...input.values];
  const disclosureIndex = values.findIndex((value) => value.toLowerCase().startsWith("for the above:"));
  if (disclosureIndex >= 0) {
    values = values.slice(0, disclosureIndex);
  }

  const totalsIndex = values.indexOf("Totals");
  if (totalsIndex >= 0) {
    const fillValues = values.slice(0, totalsIndex);
    const totals = values.slice(totalsIndex + 1);
    if (fillValues.length % 4 !== 0 || totals.length < 4) {
      throw new IngestError(`${input.where}: 无法识别多价格成交结构`);
    }
    const totalFeesRaw = totals.at(-2);
    const totalQuantityRaw = totals[0];
    const totalAmountRaw = totals.at(-1);
    if (totalFeesRaw === undefined || totalQuantityRaw === undefined || totalAmountRaw === undefined) {
      throw new IngestError(`${input.where}: 多价格 Totals 字段缺失`);
    }
    const totalFees = parseEmailDecimal(totalFeesRaw, input.where);
    if (!totalFees.equals(0)) {
      throw new IngestError(`${input.where}: 多价格成交费用无法可靠分摊: ${totalFees}`);
    }

    const rows: EmailTrade[] = [];
    for (let offset = 0; offset < fillValues.length; offset += 4) {
      const [quantity, price, principal, amount] = fillValues.slice(offset, offset + 4);
      if (quantity === undefined || price === undefined || principal === undefined || amount === undefined) {
        throw new IngestError(`${input.where}: 多价格成交行字段缺失`);
      }
      rows.push(buildTrade({ ...input, quantity, price, principal, fees: new Decimal(0), amount }));
    }
    const quantityTotal = rows.reduce((sum, row) => sum.plus(row.quantity), new Decimal(0));
    if (!quantityTotal.equals(parseEmailDecimal(totalQuantityRaw, input.where))) {
      throw new IngestError(`${input.where}: 多价格成交数量与 Totals 不一致`);
    }
    const amountTotal = rows.reduce((sum, row) => sum.plus(row.amount.abs()), new Decimal(0));
    if (!amountTotal.equals(parseEmailDecimal(totalAmountRaw, input.where))) {
      throw new IngestError(`${input.where}: 多价格成交金额与 Totals 不一致`);
    }
    return rows;
  }

  if (values.length < 4) {
    throw new IngestError(`${input.where}: 无法识别成交金额结构: ${JSON.stringify(values)}`);
  }
  const [quantity, price, principal, ...tail] = values;
  if (quantity === undefined || price === undefined || principal === undefined) {
    throw new IngestError(`${input.where}: 成交金额字段缺失`);
  }

  let fees: Decimal;
  let amount: string;
  if (tail.length === 1 && isMoney(tail[0])) {
    fees = new Decimal(0);
    amount = tail[0] as string;
  } else if (tail.length === 2 && tail[0]?.replace(/:$/, "").toLowerCase() === "n/a" && isMoney(tail[1])) {
    fees = new Decimal(0);
    amount = tail[1] as string;
  } else if (
    tail.length === 4
    && tail[0]?.toLowerCase() === "n/a:"
    && tail[2]?.toLowerCase() === "n/a"
    && isMoney(tail[1])
    && isMoney(tail[3])
  ) {
    fees = parseEmailDecimal(tail[1] as string, input.where);
    if (!fees.equals(0)) {
      throw new IngestError(`${input.where}: N/A 费用必须为 0，实际 ${fees}`);
    }
    amount = tail[3] as string;
  } else {
    ({ fees, amount } = parseLabeledFees(tail, input.where));
  }

  return [buildTrade({ ...input, quantity, price, principal, fees, amount })];
}

/** 严格解析 Commission、Industry Fee、Total 或仅 Industry Fee 的费用区域。 */
function parseLabeledFees(values: readonly string[], where: string): { fees: Decimal; amount: string } {
  let index = 0;
  let commission: Decimal;
  const industryOnly = values[0]?.toLowerCase() === "industry fee:";
  if (industryOnly) {
    commission = new Decimal(0);
  } else if (values[index]?.toLowerCase() === "commission:") {
    index += 1;
    const rawCommission = values[index];
    if (!isMoney(rawCommission)) {
      throw new IngestError(`${where}: Commission 后缺少金额`);
    }
    commission = parseEmailDecimal(rawCommission, where);
    index += 1;
    if (values[index]?.toLowerCase() === "commission") {
      index += 1;
    }
  } else if (isMoney(values[index])) {
    commission = parseEmailDecimal(values[index] as string, where);
    index += 1;
  } else {
    throw new IngestError(`${where}: 无法识别 Commission 字段: ${JSON.stringify(values)}`);
  }

  if (values[index]?.toLowerCase() !== "industry fee:") {
    throw new IngestError(`${where}: 缺少 Industry Fee 标签`);
  }
  index += 1;
  const rawIndustryFee = values[index];
  if (!isMoney(rawIndustryFee)) {
    throw new IngestError(`${where}: Industry Fee 后缺少金额`);
  }
  const industryFee = parseEmailDecimal(rawIndustryFee, where);
  index += 1;

  if (industryOnly) {
    if (values[index]?.toLowerCase() === "industry fee") {
      index += 1;
    }
    const amount = values[index];
    if (!isMoney(amount)) {
      throw new IngestError(`${where}: Industry Fee 后缺少成交净额`);
    }
    index += 1;
    if (index !== values.length) {
      throw new IngestError(`${where}: 费用表包含未知字段: ${JSON.stringify(values.slice(index))}`);
    }
    return { fees: industryFee, amount };
  }

  if (values[index]?.toLowerCase() !== "total:") {
    throw new IngestError(`${where}: 缺少费用 Total 标签`);
  }
  index += 1;
  const rawTotalFees = values[index];
  if (!isMoney(rawTotalFees)) {
    throw new IngestError(`${where}: Total 后缺少费用金额`);
  }
  const totalFees = parseEmailDecimal(rawTotalFees, where);
  index += 1;
  const amount = values[index];
  if (!isMoney(amount)) {
    throw new IngestError(`${where}: 费用表后缺少成交净额`);
  }
  index += 1;
  if (index !== values.length) {
    throw new IngestError(`${where}: 费用表包含未知字段: ${JSON.stringify(values.slice(index))}`);
  }

  const fees = commission.plus(industryFee);
  if (!fees.equals(totalFees)) {
    throw new IngestError(`${where}: 分项费用与 Total 不一致`);
  }
  return { fees, amount };
}

/** 构造单笔成交并核对 Principal、Total Amount 和买卖符号。 */
function buildTrade(input: {
  tradeDate: LocalDate;
  action: PositionAction;
  symbol: string;
  description: string;
  multiplier: number;
  where: string;
  quantity: string;
  price: string;
  principal: string;
  fees: Decimal;
  amount: string;
  values?: readonly string[];
}): EmailTrade {
  const quantity = parseEmailDecimal(input.quantity, input.where);
  const price = parseEmailDecimal(input.price, input.where);
  const principal = parseEmailDecimal(input.principal, input.where);
  const netAmount = parseEmailDecimal(input.amount, input.where);
  const expectedPrincipal = quantity.mul(price).mul(input.multiplier);
  if (principal.minus(expectedPrincipal).abs().greaterThan("0.011")) {
    throw new IngestError(
      `${input.where}: Principal 勾稽不符，实际 ${principal}，期望 ${expectedPrincipal}`,
    );
  }
  const isBuy = input.action === "Buy" || input.action === "Buy to Open" || input.action === "Buy to Close";
  const expectedNet = isBuy ? principal.plus(input.fees) : principal.minus(input.fees);
  if (netAmount.minus(expectedNet).abs().greaterThan("0.011")) {
    throw new IngestError(
      `${input.where}: Total Amount 勾稽不符，实际 ${netAmount}，期望 ${expectedNet}`,
    );
  }
  return {
    tradeDate: input.tradeDate,
    action: input.action,
    symbol: input.symbol,
    description: input.description,
    quantity,
    price,
    fees: input.fees,
    amount: isBuy ? netAmount.negated() : netAmount,
  };
}

/** 返回主正文和去重后的非空候选，主正文始终优先。 */
function candidateBodies(message: GmailMessage): readonly string[] {
  const unique: string[] = [];
  for (const candidate of [message.body, ...(message.bodyCandidates ?? [])]) {
    if (candidate.trim().length > 0 && !unique.includes(candidate)) {
      unique.push(candidate);
    }
  }
  return unique;
}

/** 从交易块读取单行字段。 */
function field(block: string, label: string, where: string): string {
  const pattern = new RegExp(`(?:^|\\n)\\s*${escapeRegex(label)}:\\s*\\n+\\s*([^\\n]+)`, "i");
  const value = pattern.exec(block)?.[1]?.trim();
  if (value === undefined) {
    throw new IngestError(`${where}: 缺少字段 '${label}'`);
  }
  return value;
}

/** 移除 Gmail Markdown 链接，仅保留合约代码。 */
function unwrapSymbol(raw: string): string {
  const match = /^\[([^\]]+)]\([^\n]+\)/.exec(raw.trim());
  return (match?.[1] ?? raw.trim()).trim();
}

/** 解析 eConfirm 的 `MM/DD/YY` 日期，二位年份属于 2000 年代。 */
function parseTradeDate(raw: string, where: string): LocalDate {
  const parts = raw.trim().split("/");
  if (parts.length !== 3) {
    throw new IngestError(`${where}: 无法解析交易日期 '${raw}'`);
  }
  const [monthRaw, dayRaw, yearRaw] = parts;
  const month = Number(monthRaw);
  const day = Number(dayRaw);
  const yearValue = Number(yearRaw);
  const year = yearValue < 100 ? yearValue + 2000 : yearValue;
  const date = new Date(Date.UTC(year, month - 1, day));
  if (!Number.isInteger(month) || !Number.isInteger(day) || !Number.isInteger(yearValue)
    || date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) {
    throw new IngestError(`${where}: 无法解析交易日期 '${raw}'`);
  }
  return `${year.toString().padStart(4, "0")}-${month.toString().padStart(2, "0")}-${day.toString().padStart(2, "0")}`;
}

/** 严格解析邮件中的美元或普通十进制值。 */
function parseEmailDecimal(raw: string, where: string): Decimal {
  const text = raw.trim().replaceAll("$", "").replaceAll(",", "");
  try {
    return new Decimal(text);
  } catch (error: unknown) {
    if (error instanceof Error) {
      throw new IngestError(`${where}: 无法解析数值 '${raw}'`);
    }
    throw error;
  }
}

/** 把期权邮件描述转换为官方 Transactions CSV 风格。 */
function normalizeDescription(symbol: string, securityDescription: string): string {
  const option = parseOptionSymbol(symbol);
  if (option === null) {
    return securityDescription.trim();
  }
  const suffix = /\s+\d{2}\/\d{2}\/\d{4}\s+\$[\d.]+\s+(?:Call|Put)$/i;
  const company = securityDescription.trim().replace(suffix, "");
  const kind = option.optionType === "C" ? "CALL" : "PUT";
  const [year, month, day] = option.expiry.split("-");
  if (year === undefined || month === undefined || day === undefined) {
    throw new IngestError(`期权到期日无法格式化: ${option.expiry}`);
  }
  return `${kind} ${company} $${plainDecimal(option.strike)} EXP ${month}/${day}/${year.slice(2)}`;
}

/** 输出无科学计数法且不保留无意义尾零的十进制文本。 */
function plainDecimal(value: Decimal): string {
  return value.toFixed();
}

/** 输出符号位位于美元符号之前的 Schwab 金额文本。 */
function signedUsd(value: Decimal): string {
  const prefix = value.isNegative() ? "-" : "";
  return `${prefix}$${plainDecimal(value.abs())}`;
}

/** 判断字段是否为 eConfirm 支持的美元文本。 */
function isMoney(value: string | undefined): value is string {
  return value !== undefined && MONEY_PATTERN.test(value);
}

/** 转义动态正则标签。 */
function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** 用精确十进制字符串比较多个正文候选的交易结果。 */
function tradesFingerprint(trades: readonly EmailTrade[]): string {
  return JSON.stringify(trades.map((trade) => ({
    ...trade,
    quantity: trade.quantity.toString(),
    price: trade.price.toString(),
    fees: trade.fees.toString(),
    amount: trade.amount.toString(),
  })));
}
