import type { ImportAccountRecord } from "./import-account.js";
import { ApplicationError } from "./app-error.js";

/** Gmail 账户后缀已经由用户确认并持久化的映射。 */
export interface GmailAccountMappingRecord {
  connectionId: string;
  accountSuffix: string;
  accountId: string;
}

/** 同步流程可直接使用的账户身份及可选新增映射。 */
export interface ResolvedGmailAccountMapping {
  accountId: string;
  legacyAccountKey: string;
  matchedBy: "existing_mapping" | "explicit_selection";
  /** 仅首次显式选择时返回；已有映射不产生重复写入。 */
  mappingToCreate: GmailAccountMappingRecord | null;
}

/** 用户显式改绑后供仓储比较并交换写入的最小计划。 */
export interface GmailAccountMappingUpdatePlan {
  expectedAccountId: string;
  nextMapping: GmailAccountMappingRecord;
  changed: boolean;
}

/**
 * 解析 eConfirm 账户尾号对应的 Lyra 账户，不依据唯一候选自动建立映射。
 *
 * 已持久化映射只在目标账户仍活跃时复用；首次出现的后缀必须由 App 明确提交
 * 账户 UUID。普通同步不能顺带改写已有映射，避免历史邮件被静默归到另一账户。
 *
 * @param input - Gmail 连接、邮件尾号、账户投影、已有映射和用户显式选择。
 * @returns 同步可用的账户身份，以及需要首次持久化的映射记录。
 */
export function resolveGmailAccountMapping(input: {
  connectionId: string;
  accountSuffix: string;
  accounts: readonly ImportAccountRecord[];
  existingMapping: GmailAccountMappingRecord | null;
  explicitAccountId: string | null;
}): ResolvedGmailAccountMapping {
  const connectionId = requireText(input.connectionId, "Gmail 连接 ID");
  const accountSuffix = requireText(input.accountSuffix, "Gmail 账户尾号").toUpperCase();
  const activeAccounts = input.accounts.filter((account) => account.status === "active");

  if (input.existingMapping !== null) {
    const mappingMatchesRequest = input.existingMapping.connectionId === connectionId
      && input.existingMapping.accountSuffix.trim().toUpperCase() === accountSuffix;
    const mappedAccount = activeAccounts.find((account) => (
      account.id === input.existingMapping?.accountId
    ));
    const explicitMatches = input.explicitAccountId === null
      || input.explicitAccountId === input.existingMapping.accountId;
    if (!mappingMatchesRequest || mappedAccount === undefined || !explicitMatches) {
      throw mappingRequired(activeAccounts, accountSuffix);
    }
    return {
      accountId: mappedAccount.id,
      legacyAccountKey: mappedAccount.legacyAccountKey,
      matchedBy: "existing_mapping",
      mappingToCreate: null,
    };
  }

  if (input.explicitAccountId !== null) {
    const selected = activeAccounts.find((account) => (
      account.id === input.explicitAccountId && suffixMatches(account, accountSuffix)
    ));
    if (selected !== undefined) {
      return {
        accountId: selected.id,
        legacyAccountKey: selected.legacyAccountKey,
        matchedBy: "explicit_selection",
        mappingToCreate: {
          connectionId,
          accountSuffix,
          accountId: selected.id,
        },
      };
    }
  }

  throw mappingRequired(activeAccounts, accountSuffix);
}

/**
 * 为映射管理页的用户显式账户改绑生成更新计划。
 *
 * 普通 Gmail 同步继续只调用 `resolveGmailAccountMapping`，不能进入此路径。仓储
 * 必须用 `expectedAccountId` 做比较并交换，只更新映射本身，不回写历史邮件或
 * 已进入账本的交易。相同目标视为幂等操作。
 *
 * @param input - 当前连接、已读取映射、账户快照和用户明确选择的目标账户。
 * @returns 当前账户前置条件与待持久化映射。
 */
export function planGmailAccountMappingUpdate(input: {
  connectionId: string;
  mapping: GmailAccountMappingRecord;
  accounts: readonly ImportAccountRecord[];
  targetAccountId: string;
}): GmailAccountMappingUpdatePlan {
  const connectionId = requireText(input.connectionId, "Gmail 连接 ID");
  const targetAccountId = requireText(input.targetAccountId, "Gmail 目标账户 ID");
  const accountSuffix = requireText(
    input.mapping.accountSuffix,
    "Gmail 账户尾号",
  ).toUpperCase();
  if (input.mapping.connectionId !== connectionId) {
    throw new ApplicationError({
      code: "INVALID_ARGUMENT",
      message: "Gmail 账户映射不属于当前连接",
      statusCode: 400,
      retryable: false,
    });
  }

  const activeAccounts = input.accounts.filter((account) => account.status === "active");
  const target = activeAccounts.find((account) => (
    account.id === targetAccountId && suffixMatches(account, accountSuffix)
  ));
  if (target === undefined) {
    throw mappingRequired(activeAccounts, accountSuffix);
  }

  if (input.mapping.accountId === target.id) {
    return {
      expectedAccountId: input.mapping.accountId,
      nextMapping: input.mapping,
      changed: false,
    };
  }
  return {
    expectedAccountId: input.mapping.accountId,
    nextMapping: {
      connectionId,
      accountSuffix,
      accountId: target.id,
    },
    changed: true,
  };
}

/** 尾号仅用于过滤可选账户，绝不等同于自动授权映射。 */
function suffixMatches(account: ImportAccountRecord, suffix: string): boolean {
  return account.maskedSuffix.trim().toUpperCase() === suffix
    || account.legacyAccountKey.toUpperCase().endsWith(suffix);
}

/** 返回可安全展示的候选账户 UUID，不泄露完整券商账户键。 */
function mappingRequired(
  accounts: readonly ImportAccountRecord[],
  suffix: string,
): ApplicationError {
  const candidateAccountIds = accounts
    .filter((account) => suffixMatches(account, suffix))
    .map((account) => account.id)
    .toSorted();
  return new ApplicationError({
    code: "ACCOUNT_MAPPING_REQUIRED",
    message: "Gmail 账户尾号必须明确映射到一个活跃账户",
    statusCode: 409,
    retryable: false,
    details: { candidate_account_ids: candidateAccountIds },
  });
}

/** 去除边界空白并拒绝空业务标识。 */
function requireText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}
