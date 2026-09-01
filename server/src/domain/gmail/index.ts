/** Gmail/eConfirm 领域模块公共导出。 */
export {
  SCHWAB_ECONFIRM_SENDER,
  SCHWAB_ECONFIRM_SUBJECT,
  SCHWAB_SENDER_PATTERN,
  emailTradeToRawFields,
  extractAccountSuffix,
  parseEconfirm,
} from "./econfirm.js";
export type { EmailTrade, GmailMessage } from "./econfirm.js";
export {
  reconcileEmailTrades,
  resolveEmailAccount,
} from "./reconciliation.js";
export {
  GmailSyncError,
  GMAIL_SYNC_ERROR_CODES,
  completeGmailSync,
  decideGmailIncrementalFetch,
  markGmailHistoryExpired,
  isGmailSyncErrorCode,
  normalizeGmailHistoryId,
  normalizeGmailSyncTimestamp,
  planGmailSync,
} from "./sync-state.js";
export {
  DEFAULT_GMAIL_SYNC_INTERVAL_MINUTES,
  planScheduledGmailSync,
} from "./sync-schedule.js";
export { completeGmailSyncBatch } from "./sync-batch.js";
export type {
  EmailReconciliationResult,
  EmailReconciliationRow,
  EmailTradeSource,
} from "./reconciliation.js";
export type {
  GmailSyncRequest,
  GmailSyncErrorCode,
  GmailSyncErrorOptions,
  GmailIncrementalFetchDecision,
  GmailIncrementalFetchObservation,
  GmailSyncState,
  GmailSyncStatus,
} from "./sync-state.js";
export type { GmailScheduledSyncPlan } from "./sync-schedule.js";
export type {
  CompletedGmailSyncBatch,
  DurableGmailMessageOutcomeStatus,
  GmailSyncMessageOutcome,
} from "./sync-batch.js";
