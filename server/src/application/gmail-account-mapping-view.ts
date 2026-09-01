import type { GmailAccountMappingRecord } from "./gmail-account-mapping.js";

/** 仓储读取的完整 Gmail 账户映射记录。 */
export interface PersistedGmailAccountMappingRecord extends GmailAccountMappingRecord {
  id: string;
  createdAt: string;
  updatedAt: string;
}

/** App 可管理的 Gmail 账户映射，不重复公开路由中的连接 ID。 */
export interface PublicGmailAccountMapping {
  id: string;
  accountSuffix: string;
  accountId: string;
  createdAt: string;
  updatedAt: string;
}

/**
 * 创建指定 Gmail 连接的安全公开映射列表。
 *
 * @param connectionId - 当前已授权路由中的 Gmail 连接 ID。
 * @param mappings - 仓储按连接查询得到的完整映射记录。
 * @returns 按账户尾号和不可变映射 ID 稳定排序的公开列表。
 */
export function createPublicGmailAccountMappingList(
  connectionId: string,
  mappings: readonly PersistedGmailAccountMappingRecord[],
): readonly PublicGmailAccountMapping[] {
  if (mappings.some((mapping) => mapping.connectionId !== connectionId)) {
    throw new RangeError("Gmail 账户映射不属于当前连接");
  }
  return mappings
    .toSorted((left, right) => (
      left.accountSuffix.localeCompare(right.accountSuffix)
      || left.id.localeCompare(right.id)
    ))
    .map((mapping) => ({
      id: mapping.id,
      accountSuffix: mapping.accountSuffix,
      accountId: mapping.accountId,
      createdAt: mapping.createdAt,
      updatedAt: mapping.updatedAt,
    }));
}
