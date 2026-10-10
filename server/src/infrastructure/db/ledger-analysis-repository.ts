import { Decimal } from "decimal.js";

import { ApplicationError } from "../../application/app-error.js";
import type { BrokerageAccount } from "../../domain/accounts/index.js";
import type { Lot, RealizedRecord } from "../../domain/fifo/index.js";
import type {
  LedgerSourceType,
  ParsedTransaction,
  PositionAction,
  TransactionAction,
} from "../../domain/ledger/index.js";
import type { LyraDatabase } from "./database.js";

/** 一个账本版本对应的账户、交易和持久化 FIFO 派生数据。 */
export interface LedgerAnalysisSnapshot {
  ledgerVersion: number;
  accounts: readonly BrokerageAccount[];
  transactions: readonly ParsedTransaction[];
  openLots: readonly Lot[];
  realizedRecords: readonly RealizedRecord[];
}

/**
 * 从 DuckDB 读取一次可供确定性金融分析使用的完整快照。
 *
 * 所有集合和当前账本版本由同一条 SQL 语句返回，因此不会在共享连接上通过
 * 多次查询拼出跨版本数据。若 Agent 会话绑定的版本已经落后，调用会明确失败，
 * 由上层在回合边界刷新会话基线后重新查询。
 *
 * @param database - 已完成 v1–v10 migration 的数据库。
 * @param expectedLedgerVersion - Agent 会话中固化的权威账本版本。
 * @returns 当前版本的账户、标准交易、未平批次和已实现结果。
 */
export async function loadLedgerAnalysisSnapshot(
  database: LyraDatabase,
  expectedLedgerVersion: number,
): Promise<LedgerAnalysisSnapshot> {
  if (!Number.isSafeInteger(expectedLedgerVersion) || expectedLedgerVersion < 0) {
    throw new RangeError("Agent 账本版本必须是非负安全整数");
  }

  const reader = await database.connection.runAndReadAll(`
    SELECT
      coalesce((SELECT max(version) FROM ledger_versions), 0) AS ledger_version,
      coalesce((
        SELECT list(struct_pack(
          id := CAST(id AS VARCHAR),
          display_name := display_name,
          legacy_account_key := legacy_account_key,
          broker := broker,
          masked_suffix := masked_suffix,
          base_currency := base_currency,
          status := status,
          created_at := CAST(created_at AS VARCHAR),
          updated_at := CAST(updated_at AS VARCHAR)
        ) ORDER BY status, created_at, id)
        FROM accounts
      ), []) AS accounts,
      coalesce((
        SELECT list(struct_pack(
          account := a.legacy_account_key,
          source_type := t.source_type,
          source_reference := t.source_reference,
          record_date := CAST(t.record_date AS VARCHAR),
          transaction_date := CAST(t.transaction_date AS VARCHAR),
          action := t.action,
          raw_symbol := t.raw_symbol,
          description := t.description,
          asset_type := t.asset_type,
          underlying := t.underlying,
          expiry := CAST(t.expiry AS VARCHAR),
          strike := CAST(t.strike AS VARCHAR),
          option_type := t.option_type,
          quantity := CAST(t.quantity AS VARCHAR),
          price := CAST(t.price AS VARCHAR),
          fees := CAST(t.fees AS VARCHAR),
          amount := CAST(t.amount AS VARCHAR),
          source_hash := t.source_hash,
          sequence := t.sequence,
          occurrence := t.occurrence,
          business_hash := t.business_hash,
          transaction_hash := t.transaction_hash
        ) ORDER BY t.transaction_date, t.sequence, t.account_id, t.transaction_hash)
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
      ), []) AS transactions,
      coalesce((
        SELECT list(struct_pack(
          account := a.legacy_account_key,
          symbol_key := l.symbol_key,
          asset_type := l.asset_type,
          underlying := l.underlying,
          expiry := CAST(l.expiry AS VARCHAR),
          strike := CAST(l.strike AS VARCHAR),
          option_type := l.option_type,
          direction := l.direction,
          open_date := CAST(l.open_date AS VARCHAR),
          open_price := CAST(l.open_price AS VARCHAR),
          open_quantity := CAST(l.open_quantity AS VARCHAR),
          remaining_quantity := CAST(l.remaining_quantity AS VARCHAR),
          open_fees := CAST(l.open_fees AS VARCHAR),
          fees_allocated := CAST(l.fees_allocated AS VARCHAR),
          open_transaction := l.open_transaction_hash
        ) ORDER BY l.account_id, l.lot_id)
        FROM lots l
        JOIN accounts a ON a.id = l.account_id
      ), []) AS open_lots,
      coalesce((
        SELECT list(struct_pack(
          account := a.legacy_account_key,
          symbol_key := r.symbol_key,
          asset_type := r.asset_type,
          underlying := r.underlying,
          direction := r.direction,
          open_date := CAST(r.open_date AS VARCHAR),
          close_date := CAST(r.close_date AS VARCHAR),
          quantity := CAST(r.quantity AS VARCHAR),
          open_price := CAST(r.open_price AS VARCHAR),
          close_price := CAST(r.close_price AS VARCHAR),
          open_fees := CAST(r.open_fees AS VARCHAR),
          close_fees := CAST(r.close_fees AS VARCHAR),
          pnl := CAST(r.pnl AS VARCHAR),
          close_action := r.close_action,
          open_transaction := r.open_transaction_hash,
          close_transaction := r.close_transaction_hash
        ) ORDER BY r.account_id, r.realized_id)
        FROM realized r
        JOIN accounts a ON a.id = r.account_id
      ), []) AS realized_records
  `);
  const row = reader.getRowObjectsJson()[0];
  if (row === undefined) {
    throw new Error("DuckDB 账本快照查询未返回结果");
  }
  const ledgerVersion = parseNonNegativeInteger(row.ledger_version, "DuckDB 账本版本");
  if (ledgerVersion !== expectedLedgerVersion) {
    throw new ApplicationError({
      code: "LEDGER_VERSION_CONFLICT",
      message: "账本版本已变化，请刷新 Agent 会话数据基线",
      statusCode: 409,
      retryable: false,
      details: {
        expected_ledger_version: expectedLedgerVersion,
        actual_ledger_version: ledgerVersion,
      },
    });
  }

  return {
    ledgerVersion,
    accounts: requireObjectList(row.accounts, "账户").map(parseAccount),
    transactions: requireObjectList(row.transactions, "交易").map(parseTransaction),
    openLots: requireObjectList(row.open_lots, "未平批次").map(parseLot),
    realizedRecords: requireObjectList(row.realized_records, "已实现记录").map(parseRealized),
  };
}

/** 把账户 struct 映射为领域记录；数据库约束负责字段闭集。 */
function parseAccount(row: Readonly<Record<string, unknown>>): BrokerageAccount {
  return {
    id: requireString(row.id, "账户 ID"),
    displayName: requireString(row.display_name, "账户显示名"),
    legacyAccountKey: requireString(row.legacy_account_key, "账户旧键"),
    broker: requireString(row.broker, "券商") as BrokerageAccount["broker"],
    maskedSuffix: requireString(row.masked_suffix, "账户尾号"),
    baseCurrency: requireString(row.base_currency, "账户币种"),
    status: requireString(row.status, "账户状态") as BrokerageAccount["status"],
    createdAt: parseTimestamp(row.created_at, "账户创建时间"),
    updatedAt: parseTimestamp(row.updated_at, "账户更新时间"),
  };
}

/** 把标准交易 struct 恢复为 Decimal 领域对象。 */
function parseTransaction(row: Readonly<Record<string, unknown>>): ParsedTransaction {
  return {
    account: requireString(row.account, "交易账户"),
    sourceType: requireString(row.source_type, "交易来源") as LedgerSourceType,
    sourceReference: requireString(row.source_reference, "交易来源引用"),
    recordDate: requireString(row.record_date, "交易记录日期"),
    transactionDate: requireString(row.transaction_date, "交易生效日期"),
    action: requireString(row.action, "交易动作") as TransactionAction,
    rawSymbol: nullableString(row.raw_symbol, "原始代码"),
    description: nullableString(row.description, "交易描述"),
    assetType: requireString(row.asset_type, "交易资产类型") as ParsedTransaction["assetType"],
    underlying: nullableString(row.underlying, "交易标的"),
    expiry: nullableString(row.expiry, "交易到期日"),
    strike: nullableDecimal(row.strike, "交易行权价"),
    optionType: nullableString(row.option_type, "交易期权类型") as ParsedTransaction["optionType"],
    quantity: nullableDecimal(row.quantity, "交易数量"),
    price: nullableDecimal(row.price, "交易价格"),
    fees: nullableDecimal(row.fees, "交易费用"),
    amount: nullableDecimal(row.amount, "交易金额"),
    sourceHash: requireString(row.source_hash, "交易来源摘要"),
    sequence: parsePositiveInteger(row.sequence, "交易序号"),
    occurrence: parsePositiveInteger(row.occurrence, "交易出现序号"),
    businessHash: requireString(row.business_hash, "交易业务摘要"),
    transactionHash: requireString(row.transaction_hash, "交易摘要"),
  };
}

/** 把持久化未平批次恢复为 FIFO 领域对象。 */
function parseLot(row: Readonly<Record<string, unknown>>): Lot {
  return {
    account: requireString(row.account, "批次账户"),
    symbolKey: requireString(row.symbol_key, "批次合约"),
    assetType: requireString(row.asset_type, "批次资产类型") as Lot["assetType"],
    underlying: requireString(row.underlying, "批次标的"),
    expiry: nullableString(row.expiry, "批次到期日"),
    strike: nullableDecimal(row.strike, "批次行权价"),
    optionType: nullableString(row.option_type, "批次期权类型") as Lot["optionType"],
    direction: requireString(row.direction, "批次方向") as Lot["direction"],
    openDate: requireString(row.open_date, "批次开仓日期"),
    openPrice: requireDecimal(row.open_price, "批次开仓价"),
    openQuantity: requireDecimal(row.open_quantity, "批次开仓数量"),
    remainingQuantity: requireDecimal(row.remaining_quantity, "批次剩余数量"),
    openFees: requireDecimal(row.open_fees, "批次开仓费用"),
    feesAllocated: requireDecimal(row.fees_allocated, "批次已分摊费用"),
    openTransaction: requireString(row.open_transaction, "批次开仓交易"),
  };
}

/** 把持久化已实现记录恢复为 FIFO 领域对象。 */
function parseRealized(row: Readonly<Record<string, unknown>>): RealizedRecord {
  return {
    account: requireString(row.account, "已实现账户"),
    symbolKey: requireString(row.symbol_key, "已实现合约"),
    assetType: requireString(row.asset_type, "已实现资产类型") as RealizedRecord["assetType"],
    underlying: requireString(row.underlying, "已实现标的"),
    direction: requireString(row.direction, "已实现方向") as RealizedRecord["direction"],
    openDate: requireString(row.open_date, "已实现开仓日期"),
    closeDate: requireString(row.close_date, "已实现平仓日期"),
    quantity: requireDecimal(row.quantity, "已实现数量"),
    openPrice: requireDecimal(row.open_price, "已实现开仓价"),
    closePrice: requireDecimal(row.close_price, "已实现平仓价"),
    openFees: requireDecimal(row.open_fees, "已实现开仓费用"),
    closeFees: requireDecimal(row.close_fees, "已实现平仓费用"),
    pnl: requireDecimal(row.pnl, "已实现损益"),
    closeAction: requireString(row.close_action, "已实现平仓动作") as PositionAction,
    openTransaction: requireString(row.open_transaction, "已实现开仓交易"),
    closeTransaction: requireString(row.close_transaction, "已实现平仓交易"),
  };
}

/** DuckDB LIST<STRUCT> 在 JSON 边界必须保持对象数组。 */
function requireObjectList(
  value: unknown,
  label: string,
): readonly Readonly<Record<string, unknown>>[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "object" || item === null)) {
    throw new Error(`${label}快照与数据库 schema 不一致`);
  }
  return value as readonly Readonly<Record<string, unknown>>[];
}

/** 读取 DuckDB JSON 的必填字符串字段。 */
function requireString(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new Error(`${label}与数据库 schema 不一致`);
  }
  return value;
}

/** 读取允许为空的字符串字段。 */
function nullableString(value: unknown, label: string): string | null {
  if (value === null) {
    return null;
  }
  return requireString(value, label);
}

/** 精确恢复 DuckDB 已显式转成字符串的 Decimal。 */
function requireDecimal(value: unknown, label: string): Decimal {
  const raw = requireString(value, label);
  try {
    return new Decimal(raw);
  } catch (error: unknown) {
    throw new RangeError(`${label}不是有效十进制数`, { cause: error });
  }
}

/** 精确恢复可空 Decimal。 */
function nullableDecimal(value: unknown, label: string): Decimal | null {
  return value === null ? null : requireDecimal(value, label);
}

/** DuckDB INTEGER/BIGINT JSON 值统一收窄为非负安全整数。 */
function parseNonNegativeInteger(value: unknown, label: string): number {
  const parsed = typeof value === "number"
    ? value
    : typeof value === "string" && /^\d+$/u.test(value)
      ? Number(value)
      : Number.NaN;
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new RangeError(`${label}必须是非负安全整数`);
  }
  return parsed;
}

/** 交易物理序号必须严格为正。 */
function parsePositiveInteger(value: unknown, label: string): number {
  const parsed = parseNonNegativeInteger(value, label);
  if (parsed === 0) {
    throw new RangeError(`${label}必须是正整数`);
  }
  return parsed;
}

/** 把 DuckDB TIMESTAMPTZ 文本规范为 UTC ISO 时间。 */
function parseTimestamp(value: unknown, label: string): string {
  const raw = requireString(value, label);
  const timestamp = new Date(raw);
  if (Number.isNaN(timestamp.getTime())) {
    throw new RangeError(`${label}无效`);
  }
  return timestamp.toISOString();
}
