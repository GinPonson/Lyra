import type { AccountScope } from "../domain/analytics/index.js";
import { ApplicationError } from "./app-error.js";

/** 解析账户范围所需的最小持久化账户投影。 */
export interface AccountScopeRecord {
  id: string;
  legacyAccountKey: string;
  baseCurrency: string;
  status: "active" | "archived";
}

/** 同一基础币种下可以安全合计的账户组。 */
export interface AccountCurrencyGroup {
  currency: string;
  accountIds: readonly string[];
}

/** API 账户范围解析为查询层账户键后的授权结果。 */
export interface ResolvedAccountScope {
  requestedScope: AccountScope;
  /** 供现有 TypeScript 账本内核过滤的旧账户键范围。 */
  ledgerAccountKeys: readonly string[];
  accountIds: readonly string[];
  currencyGroups: readonly AccountCurrencyGroup[];
  /** 仅当范围内账户同币种时有值；多币种必须按 currencyGroups 展示。 */
  aggregateCurrency: string | null;
}

/**
 * 把 App 显式提交的账户 UUID 范围解析为受权账本账户键。
 *
 * `all` 只选择活跃账户；归档账户仍可通过其 UUID 显式查询历史。函数不会把
 * 空范围解释成全部账户，也不会在多币种时声明可直接合计的币种。
 *
 * @param requestedScope - App 请求中的单账户 UUID 或明确的 all。
 * @param accounts - 当前服务端可见账户投影。
 */
export function resolveAccountScope(
  requestedScope: AccountScope | undefined,
  accounts: readonly AccountScopeRecord[],
): ResolvedAccountScope {
  if (requestedScope === undefined) {
    throw new ApplicationError({
      code: "ACCOUNT_SCOPE_REQUIRED",
      message: "请求必须明确指定单个账户或全部活跃账户",
      statusCode: 400,
      retryable: false,
    });
  }

  const selected = requestedScope.kind === "all"
    ? accounts.filter((account) => account.status === "active")
    : selectExplicitAccount(requestedScope.account, accounts);
  const sorted = selected.toSorted((left, right) => left.id.localeCompare(right.id));
  const groups = groupAccountsByCurrency(sorted);

  return {
    requestedScope,
    ledgerAccountKeys: sorted.map((account) => account.legacyAccountKey),
    accountIds: sorted.map((account) => account.id),
    currencyGroups: groups,
    aggregateCurrency: groups.length === 1 ? groups[0]?.currency ?? null : null,
  };
}

/** 单账户范围必须使用内部 UUID 精确命中，不能用后缀或旧键猜测。 */
function selectExplicitAccount(
  accountId: string,
  accounts: readonly AccountScopeRecord[],
): readonly AccountScopeRecord[] {
  const account = accounts.find((candidate) => candidate.id === accountId);
  if (account === undefined) {
    throw new ApplicationError({
      code: "INVALID_ARGUMENT",
      message: "指定账户不存在",
      statusCode: 400,
      retryable: false,
      details: { field: "account_scope.account" },
    });
  }
  return [account];
}

/** 按 ISO 币种建立稳定排序的账户组，供 API 阻止跨币种伪总计。 */
function groupAccountsByCurrency(
  accounts: readonly AccountScopeRecord[],
): readonly AccountCurrencyGroup[] {
  const accountIdsByCurrency = new Map<string, string[]>();
  for (const account of accounts) {
    const ids = accountIdsByCurrency.get(account.baseCurrency) ?? [];
    ids.push(account.id);
    accountIdsByCurrency.set(account.baseCurrency, ids);
  }
  return [...accountIdsByCurrency.entries()]
    .toSorted(([left], [right]) => left.localeCompare(right))
    .map(([currency, accountIds]) => ({ currency, accountIds }));
}
