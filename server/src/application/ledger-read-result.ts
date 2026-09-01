import type { AccountScope } from "../domain/analytics/index.js";
import type { ResolvedAccountScope } from "./account-scope.js";

/** App 缓存和页面口径共用的权威账本元数据。 */
export interface LedgerReadMetadata {
  accountScope: AccountScope;
  accountIds: readonly string[];
  currencyGroups: readonly {
    currency: string;
    accountIds: readonly string[];
  }[];
  /** 多币种范围为 null，客户端不得把不同币种合并成一个金额。 */
  aggregateCurrency: string | null;
  ledgerVersion: number;
  asOf: string;
  fetchedAt: string;
}

/** 只读账本 API 的统一结果形状；金融数据仍由既有领域查询生成。 */
export interface LedgerReadResult<T> {
  metadata: LedgerReadMetadata;
  data: T;
}

/**
 * 为 App 只读查询附加缓存校验和页面展示所需的权威数据语境。
 *
 * 函数不复制或重新计算金融结果，只固定已解析的授权账户范围、币种分组、账本
 * 版本与 UTC 时间。调用方必须传入当前读快照对应的版本，不能用请求参数覆盖。
 *
 * @param input.data - 既有领域查询已经计算完成的结果。
 * @param input.scope - 服务端根据设备可见账户解析出的权威范围。
 * @param input.ledgerVersion - 当前 DuckDB 读快照的非负版本；空账本允许为 0。
 * @param input.asOf - 数据基线时间，必须显式包含时区。
 * @param input.fetchedAt - 服务端实际完成读取的时间。
 * @returns 可直接交给 App 的带元数据只读结果。
 */
export function createLedgerReadResult<T>(input: {
  data: T;
  scope: ResolvedAccountScope;
  ledgerVersion: number;
  asOf: string;
  fetchedAt: Date;
}): LedgerReadResult<T> {
  if (!Number.isSafeInteger(input.ledgerVersion) || input.ledgerVersion < 0) {
    throw new RangeError("账本版本必须是非负安全整数");
  }

  return {
    metadata: {
      accountScope: input.scope.requestedScope.kind === "all"
        ? { kind: "all" }
        : { kind: "account", account: input.scope.requestedScope.account },
      accountIds: [...input.scope.accountIds],
      currencyGroups: input.scope.currencyGroups.map((group) => ({
        currency: group.currency,
        accountIds: [...group.accountIds],
      })),
      aggregateCurrency: input.scope.aggregateCurrency,
      ledgerVersion: input.ledgerVersion,
      asOf: normalizeUtcTimestamp(input.asOf, "数据截止时间"),
      fetchedAt: normalizeUtcDate(input.fetchedAt, "查询获取时间"),
    },
    data: input.data,
  };
}

/** 接受带显式时区的 ISO 时间并统一输出为 UTC。 */
function normalizeUtcTimestamp(value: string, label: string): string {
  const normalized = value.trim();
  if (!/^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/i.test(normalized)) {
    throw new RangeError(`${label}必须是带时区的 ISO 时间`);
  }
  return normalizeUtcDate(new Date(normalized), label);
}

/** 拒绝无效服务端时钟并输出标准 UTC ISO 文本。 */
function normalizeUtcDate(value: Date, label: string): string {
  if (Number.isNaN(value.getTime())) {
    throw new RangeError(`${label}无效`);
  }
  return value.toISOString();
}
