import { ApplicationError } from "./app-error.js";

/** 导入预览账户匹配所需的最小账户投影。 */
export interface ImportAccountRecord {
  id: string;
  legacyAccountKey: string;
  maskedSuffix: string;
  status: "active" | "archived";
}

/** 文件账户线索完成显式或唯一精确匹配后的结果。 */
export interface ResolvedImportAccount {
  accountId: string;
  legacyAccountKey: string;
  matchedBy: "legacy_key" | "explicit_selection";
}

/**
 * 把 Schwab 文件名解析出的账户线索映射为 Lyra 账户。
 *
 * 未显式选择时只允许唯一的完整旧账户键自动匹配；后缀即使唯一也只作为候选
 * 返回给 App 确认。显式选择必须是活跃账户且与完整键或脱敏后缀一致。
 *
 * @param accountHint - `parseAccount` 从官方文件名得到的账户键。
 * @param accounts - 当前账户最小投影。
 * @param explicitAccountId - 用户在预览中明确选择的账户 UUID，未选择传 null。
 */
export function resolveImportAccount(
  accountHint: string,
  accounts: readonly ImportAccountRecord[],
  explicitAccountId: string | null,
): ResolvedImportAccount {
  const hint = accountHint.trim();
  if (hint.length === 0) {
    throw new RangeError("导入账户线索不能为空");
  }
  const activeAccounts = accounts.filter((account) => account.status === "active");

  if (explicitAccountId !== null) {
    const selected = activeAccounts.find((account) => account.id === explicitAccountId);
    if (selected === undefined || !accountHintMatches(selected, hint)) {
      throw mappingRequired(activeAccounts, hint);
    }
    return {
      accountId: selected.id,
      legacyAccountKey: selected.legacyAccountKey,
      matchedBy: "explicit_selection",
    };
  }

  const exactMatches = activeAccounts.filter((account) => account.legacyAccountKey === hint);
  if (exactMatches.length === 1) {
    const [matched] = exactMatches;
    if (matched !== undefined) {
      return {
        accountId: matched.id,
        legacyAccountKey: matched.legacyAccountKey,
        matchedBy: "legacy_key",
      };
    }
  }
  throw mappingRequired(activeAccounts, hint);
}

/** 显式选择允许完整旧键或脱敏后缀与文件线索对应。 */
function accountHintMatches(account: ImportAccountRecord, hint: string): boolean {
  const suffix = account.maskedSuffix.trim();
  return account.legacyAccountKey === hint
    || (suffix.length > 0 && hint.endsWith(suffix));
}

/** 返回候选 UUID，不暴露文件账户键或完整券商标识。 */
function mappingRequired(
  activeAccounts: readonly ImportAccountRecord[],
  hint: string,
): ApplicationError {
  const candidateAccountIds = activeAccounts
    .filter((account) => accountHintMatches(account, hint))
    .map((account) => account.id)
    .toSorted();
  return new ApplicationError({
    code: "ACCOUNT_MAPPING_REQUIRED",
    message: "导入文件必须明确选择匹配的活跃账户",
    statusCode: 409,
    retryable: false,
    details: { candidate_account_ids: candidateAccountIds },
  });
}
