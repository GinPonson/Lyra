import type { BrokerageAccount } from "../domain/accounts/index.js";

/** App 与 Agent `accounts` 共用的账户公开视图。 */
export interface PublicBrokerageAccount {
  id: string;
  displayName: string;
  broker: "schwab";
  maskedSuffix: string;
  baseCurrency: string;
  status: "active" | "archived";
  createdAt: string;
  updatedAt: string;
}

/**
 * 把已完成设备授权过滤的账户记录转换为公开列表。
 *
 * 迁移期 `legacyAccountKey` 只供服务端账本映射，禁止进入 App 或 Agent 结果。列表
 * 先展示活跃账户，再按创建时间和不可变 ID 排序；不会修改仓储传入的原数组。
 *
 * @param accounts - 当前设备已获授权的完整服务端账户记录。
 * @returns 不含旧账户键的稳定公开账户列表。
 */
export function createPublicAccountList(
  accounts: readonly BrokerageAccount[],
): readonly PublicBrokerageAccount[] {
  return accounts
    .toSorted((left, right) => (
      accountStatusOrder(left.status) - accountStatusOrder(right.status)
      || left.createdAt.localeCompare(right.createdAt)
      || left.id.localeCompare(right.id)
    ))
    .map((account) => ({
      id: account.id,
      displayName: account.displayName,
      broker: account.broker,
      maskedSuffix: account.maskedSuffix,
      baseCurrency: account.baseCurrency,
      status: account.status,
      createdAt: account.createdAt,
      updatedAt: account.updatedAt,
    }));
}

/** 活跃账户在选择器中优先于仅供历史查询的归档账户。 */
function accountStatusOrder(status: BrokerageAccount["status"]): number {
  return status === "active" ? 0 : 1;
}
