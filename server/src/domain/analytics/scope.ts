import type { AccountScope } from "./types.js";

/** 判断一条账户数据是否属于调用方显式指定的范围。 */
export function isAccountInScope(account: string, scope: AccountScope): boolean {
  return scope.kind === "all" || scope.account === account;
}

