import type { AccountScope } from "../domain/analytics/index.js";
import type { ResolvedAccountScope } from "./account-scope.js";
import { resolveAccountScope } from "./account-scope.js";
import { loadLedgerAnalysisSnapshot } from "../infrastructure/db/ledger-analysis-repository.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";

/**
 * 为一次只读账本请求解析权威账户范围。
 *
 * 与金融工具共用同一解析规则：all 只含活跃账户，显式账户可查询归档历史，
 * 缺省或未知账户在进入任何分析前明确拒绝。结果只用于响应元数据，金融计算
 * 仍由 Tool Registry 的既有 handler 执行。
 */
export async function resolveLedgerReadScope(
  database: LyraDatabase,
  requestedScope: AccountScope,
  ledgerVersion: number,
): Promise<ResolvedAccountScope> {
  const snapshot = await loadLedgerAnalysisSnapshot(database, ledgerVersion);
  return resolveAccountScope(requestedScope, snapshot.accounts);
}
