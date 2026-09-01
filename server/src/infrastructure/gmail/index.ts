/** Gmail 基础设施适配器公共导出。 */
export { parseGmailFullInput } from "./full-message.js";
export { parseRfcMimeMessage } from "./rfc-mime.js";
export type { ParseRfcMimeMessageInput } from "./rfc-mime.js";
export { buildSchwabEconfirmSearch } from "./search-query.js";
export {
  createGmailConnectionRecord,
  disconnectGmailConnection,
} from "./connection-credential.js";
export {
  createParsedGmailMessageMetadata,
  createUnsuccessfulGmailMessageMetadata,
} from "./message-metadata.js";
export { buildGmailMessageImportPlan } from "./message-import-plan.js";
export {
  GMAIL_HISTORY_ENDPOINT,
  GMAIL_MESSAGES_ENDPOINT,
  GMAIL_PROFILE_ENDPOINT,
  fetchBoundedGmailFullRecords,
  fetchGmailProfile,
  fetchInitialGmailFullRecords,
  fetchIncrementalGmailFullRecords,
} from "./google-gmail-client.js";
export {
  GOOGLE_GMAIL_AUTHORIZATION_SCOPES,
  GOOGLE_AUTHORIZATION_ENDPOINT,
  GOOGLE_TOKEN_ENDPOINT,
  GOOGLE_TOKEN_REVOCATION_ENDPOINT,
  GOOGLE_USERINFO_ENDPOINT,
  exchangeGoogleAuthorizationCode,
  buildGoogleAuthorizationRequest,
  refreshGmailConnectionAccessToken,
  refreshGoogleAccessToken,
  revokeGmailConnectionAuthorization,
} from "./google-oauth-client.js";
export {
  GMAIL_READONLY_SCOPE,
  GmailOAuthError,
  assertGmailReadonlyScope,
  consumeGoogleOAuthTransaction,
  createGoogleOAuthTransaction,
} from "./oauth-transaction.js";
export type {
  CreatedGoogleOAuthTransaction,
  GmailOAuthErrorCode,
  GmailOAuthErrorOptions,
  GoogleOAuthTransactionRecord,
} from "./oauth-transaction.js";
export type {
  ExchangeGoogleAuthorizationCodeInput,
  ExchangedGoogleAuthorization,
  GoogleOAuthFetch,
  GoogleOAuthClientConfig,
  GoogleAuthorizationRequest,
  RefreshGoogleAccessTokenInput,
  RefreshedGoogleAccessToken,
} from "./google-oauth-client.js";
export type {
  BoundedGmailFullRecords,
  GmailApiFetch,
  GmailIncrementalHistoryInput,
  GmailProfile,
  InitialGmailFullRecords,
  IncrementalGmailFullRecords,
} from "./google-gmail-client.js";
export type { GmailBoundedSearchPlan } from "./search-query.js";
export type {
  GmailConnectionRecord,
  GoogleOfflineGrant,
  VerifiedGoogleIdentity,
} from "./connection-credential.js";
export type {
  ParsedGmailMessageMetadata,
  UnsuccessfulGmailMessageMetadata,
} from "./message-metadata.js";
export type {
  AlreadyImportedGmailMessagePlan,
  ExistingGmailMessageState,
  GmailMessageImportPlan,
  GmailMessageTransactionLink,
  ImportedGmailMessageMetadata,
  ReadyGmailMessageImportPlan,
} from "./message-import-plan.js";
