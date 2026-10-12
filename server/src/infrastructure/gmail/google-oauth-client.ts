import type {
  GmailConnectionRecord,
  GoogleOfflineGrant,
  VerifiedGoogleIdentity,
} from "./connection-credential.js";
import { disconnectGmailConnection } from "./connection-credential.js";
import { GmailSyncError } from "../../domain/gmail/index.js";
import {
  SecretCipher,
  SecretDecryptionError,
} from "../security/index.js";
import {
  GMAIL_READONLY_SCOPE,
  GmailOAuthError,
  assertGmailReadonlyScope,
} from "./oauth-transaction.js";
import type { CreatedGoogleOAuthTransaction } from "./oauth-transaction.js";

/** Google 标准 OAuth 令牌交换端点。 */
export const GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token";

/** 使用 bearer access token 读取已验证 Google 身份的标准端点。 */
export const GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo";

/** Google OAuth 令牌撤销端点。 */
export const GOOGLE_TOKEN_REVOCATION_ENDPOINT = "https://oauth2.googleapis.com/revoke";

/** Google OAuth 2.0 系统浏览器授权端点。 */
export const GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth";

/** Android 发起授权时应申请的完整最小权限集合。 */
export const GOOGLE_GMAIL_AUTHORIZATION_SCOPES = [
  "openid",
  "email",
  GMAIL_READONLY_SCOPE,
] as const;

/** Google OAuth 单次 HTTP 调用超时，避免授权请求无限占用服务端任务。 */
const GOOGLE_OAUTH_TIMEOUT_MILLISECONDS = 15_000;

/** 授权码交换需要的短生命周期输入；所有敏感字段均不得持久化或记录。 */
export interface ExchangeGoogleAuthorizationCodeInput {
  authorizationCode: string;
  codeVerifier: string;
  clientId: string;
  clientSecret?: string;
  redirectUri: string;
  now: Date;
}

/** 后续创建 Gmail 连接及首次请求所需的最小交换结果。 */
export interface ExchangedGoogleAuthorization {
  /** 仅允许在内存中传给 Gmail API 客户端，不得写入数据库或响应 App。 */
  accessToken: string;
  accessTokenExpiresAt: string;
  identity: VerifiedGoogleIdentity;
  grant: GoogleOfflineGrant;
}

/** 后台同步使用 refresh token 换取 access token 所需的输入。 */
export interface RefreshGoogleAccessTokenInput {
  refreshToken: string;
  grantedScopeText: string;
  clientId: string;
  clientSecret?: string;
  now: Date;
}

/** 只存在于当前同步任务内存中的刷新结果。 */
export interface RefreshedGoogleAccessToken {
  accessToken: string;
  accessTokenExpiresAt: string;
  grantedScopeText: string;
}

/** Gmail 连接刷新所需的部署级 Google OAuth 配置。 */
export interface GoogleOAuthClientConfig {
  clientId: string;
  clientSecret?: string;
}

/** 可安全返回 Android App 的系统授权请求，不含 PKCE verifier。 */
export interface GoogleAuthorizationRequest {
  authorizationUrl: string;
  expiresAt: string;
}

/** 为测试替换网络边界的最小 fetch 契约。 */
export type GoogleOAuthFetch = (
  input: string | URL,
  init?: RequestInit,
) => Promise<Response>;

interface GoogleTokenResponse {
  accessToken: string;
  refreshToken: string;
  grantedScopeText: string;
  expiresInSeconds: number;
}

/** Google token HTTP 边界的内部失败，不包含响应正文。 */
class GoogleTokenEndpointFailure extends Error {
  constructor(
    readonly status: number | null,
    readonly retryable: boolean,
    message: string,
    cause?: unknown,
  ) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "GoogleTokenEndpointFailure";
  }
}

/**
 * 为 Android 系统浏览器构造 Google 离线授权 URL。
 *
 * 调用方必须把 transaction.record 仅保存在服务端；返回值只包含系统浏览器
 * URL 与过期时间，绝不暴露 codeVerifier 或 state hash。
 */
export function buildGoogleAuthorizationRequest(
  transaction: Pick<
    CreatedGoogleOAuthTransaction,
    "state" | "codeChallenge" | "codeChallengeMethod" | "record"
  >,
  clientId: string,
  redirectUri: string,
): GoogleAuthorizationRequest {
  const normalizedClientId = requireText(clientId, "Google OAuth client ID");
  const normalizedRedirectUri = requireRedirectUri(redirectUri);
  if (!/^[A-Za-z0-9_-]{43}$/.test(transaction.state)) {
    throw new RangeError("Google OAuth state 无效");
  }
  if (
    transaction.codeChallengeMethod !== "S256"
    || !/^[A-Za-z0-9_-]{43}$/.test(transaction.codeChallenge)
  ) {
    throw new RangeError("Google OAuth PKCE challenge 无效");
  }
  const expiresAt = new Date(transaction.record.expiresAt);
  if (Number.isNaN(expiresAt.getTime())) {
    throw new RangeError("Google OAuth 授权事务过期时间无效");
  }

  const url = new URL(GOOGLE_AUTHORIZATION_ENDPOINT);
  url.searchParams.set("client_id", normalizedClientId);
  url.searchParams.set("redirect_uri", normalizedRedirectUri);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("scope", GOOGLE_GMAIL_AUTHORIZATION_SCOPES.join(" "));
  url.searchParams.set("state", transaction.state);
  url.searchParams.set("code_challenge", transaction.codeChallenge);
  url.searchParams.set("code_challenge_method", transaction.codeChallengeMethod);
  url.searchParams.set("access_type", "offline");
  url.searchParams.set("prompt", "consent");
  url.searchParams.set("include_granted_scopes", "true");
  return {
    authorizationUrl: url.toString(),
    expiresAt: expiresAt.toISOString(),
  };
}

/**
 * 使用 PKCE 授权码建立 Google 离线授权，并读取已验证邮箱身份。
 *
 * @param input - 服务端已消费 OAuth state 后取得的授权码、PKCE 和客户端配置。
 * @param fetchFn - 默认使用 Node 原生 fetch；测试可注入确定性的 HTTP 边界。
 * @returns 临时 access token、离线 grant 与经过 Google userinfo 确认的身份。
 */
export async function exchangeGoogleAuthorizationCode(
  input: ExchangeGoogleAuthorizationCodeInput,
  fetchFn: GoogleOAuthFetch = globalThis.fetch,
): Promise<ExchangedGoogleAuthorization> {
  const authorizationCode = requireText(input.authorizationCode, "Google 授权码");
  const codeVerifier = requireText(input.codeVerifier, "Google PKCE verifier");
  const clientId = requireText(input.clientId, "Google OAuth client ID");
  const redirectUri = requireText(input.redirectUri, "Google OAuth redirect URI");
  const now = requireDate(input.now);

  const form = new URLSearchParams({
    code: authorizationCode,
    code_verifier: codeVerifier,
    client_id: clientId,
    redirect_uri: redirectUri,
    grant_type: "authorization_code",
  });
  if (input.clientSecret !== undefined) {
    form.set("client_secret", requireText(input.clientSecret, "Google OAuth client secret"));
  }

  const tokenResponse = await requestGoogleToken(form, fetchFn);
  const identity = await requestVerifiedGoogleIdentity(tokenResponse.accessToken, fetchFn);
  const grantedScopes = assertGmailReadonlyScope(tokenResponse.grantedScopeText);
  const accessTokenExpiresAt = calculateAccessTokenExpiry(
    now,
    tokenResponse.expiresInSeconds,
  );
  if (accessTokenExpiresAt === null) {
    throw malformedGoogleResponse("token");
  }

  return {
    accessToken: tokenResponse.accessToken,
    accessTokenExpiresAt,
    identity,
    grant: {
      refreshToken: tokenResponse.refreshToken,
      grantedScopeText: grantedScopes.join(" "),
    },
  };
}

/**
 * 使用加密存储解密出的 refresh token 获取后台同步所需的短期 access token。
 *
 * Google 刷新响应按协议可以省略 scope；此时沿用并再次校验连接记录中的
 * grantedScopeText。函数不会返回或轮换 refresh token。
 */
export async function refreshGoogleAccessToken(
  input: RefreshGoogleAccessTokenInput,
  fetchFn: GoogleOAuthFetch = globalThis.fetch,
): Promise<RefreshedGoogleAccessToken> {
  const refreshToken = requireText(input.refreshToken, "Google refresh token");
  const clientId = requireText(input.clientId, "Google OAuth client ID");
  const now = requireDate(input.now);
  let grantedScopes = assertGmailReadonlyScope(input.grantedScopeText);

  const form = new URLSearchParams({
    refresh_token: refreshToken,
    client_id: clientId,
    grant_type: "refresh_token",
  });
  if (input.clientSecret !== undefined) {
    form.set("client_secret", requireText(input.clientSecret, "Google OAuth client secret"));
  }

  let payload: Record<string, unknown>;
  try {
    payload = await postGoogleTokenForm(form, fetchFn);
  } catch (error) {
    if (!(error instanceof GoogleTokenEndpointFailure)) {
      throw error;
    }
    if (error.status === 400 || error.status === 401 || error.status === 403) {
      throw new GmailSyncError(
        "GMAIL_AUTH_REVOKED",
        "Gmail 授权已失效，需要重新授权",
        { cause: error },
      );
    }
    throw new GmailSyncError(
      "GMAIL_CONNECTION_ERROR",
      "无法刷新 Gmail 授权，请稍后重试",
      { retryable: error.retryable, cause: error },
    );
  }

  const accessToken = nonEmptyText(payload.access_token);
  const tokenType = nonEmptyText(payload.token_type);
  const expiresInSeconds = payload.expires_in;
  if (
    accessToken === null
    || tokenType?.toLowerCase() !== "bearer"
    || !Number.isSafeInteger(expiresInSeconds)
    || (expiresInSeconds as number) <= 0
  ) {
    throw invalidRefreshResponse();
  }
  if (payload.scope !== undefined) {
    const refreshedScopeText = nonEmptyText(payload.scope);
    if (refreshedScopeText === null) {
      throw invalidRefreshResponse();
    }
    grantedScopes = assertGmailReadonlyScope(refreshedScopeText);
  }
  const accessTokenExpiresAt = calculateAccessTokenExpiry(now, expiresInSeconds as number);
  if (accessTokenExpiresAt === null) {
    throw invalidRefreshResponse();
  }

  return {
    accessToken,
    accessTokenExpiresAt,
    grantedScopeText: grantedScopes.join(" "),
  };
}

/**
 * 解密连接记录中的 refresh token 并取得同步用短期 access token。
 *
 * refresh token 仅作为局部变量传给 token endpoint，不进入返回值、错误消息、
 * 日志字段或连接记录更新。AAD 与创建连接时保持 `gmail-connection:<id>` 一致。
 */
export async function refreshGmailConnectionAccessToken(
  connection: GmailConnectionRecord,
  cipher: SecretCipher,
  config: GoogleOAuthClientConfig,
  now: Date,
  fetchFn: GoogleOAuthFetch = globalThis.fetch,
): Promise<RefreshedGoogleAccessToken> {
  const refreshToken = decryptConnectionRefreshToken(connection, cipher);

  return refreshGoogleAccessToken({
    refreshToken,
    grantedScopeText: connection.grantedScopes,
    clientId: config.clientId,
    ...(config.clientSecret === undefined ? {} : { clientSecret: config.clientSecret }),
    now,
  }, fetchFn);
}

/**
 * 请求 Google 撤销 refresh token，成功后才生成本地断开连接记录。
 *
 * 与纯本地 `disconnectGmailConnection` 不同，本函数不会在远端失败时假定已经
 * 撤销，也不会提前清除本地密文，调用方可安全重试。
 */
export async function revokeGmailConnectionAuthorization(
  connection: GmailConnectionRecord,
  cipher: SecretCipher,
  now: Date,
  fetchFn: GoogleOAuthFetch = globalThis.fetch,
): Promise<GmailConnectionRecord> {
  const refreshToken = decryptConnectionRefreshToken(connection, cipher);
  const form = new URLSearchParams({ token: refreshToken });
  let response: Response;
  try {
    response = await fetchFn(GOOGLE_TOKEN_REVOCATION_ENDPOINT, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: form,
      signal: AbortSignal.timeout(GOOGLE_OAUTH_TIMEOUT_MILLISECONDS),
    });
  } catch (error) {
    throw new GmailSyncError(
      "GMAIL_CONNECTION_ERROR",
      "无法请求 Google 撤销 Gmail 授权，请稍后重试",
      { retryable: true, cause: error },
    );
  }
  if (!response.ok) {
    throw new GmailSyncError(
      "GMAIL_CONNECTION_ERROR",
      "Google 未确认 Gmail 授权撤销，请稍后重试",
      {
        retryable: response.status === 429 || response.status >= 500,
        cause: new Error(`Google revoke endpoint returned HTTP ${response.status}`),
      },
    );
  }
  return disconnectGmailConnection(connection, now);
}

/** 调用令牌端点，并把不可信响应收敛为严格内部结构。 */
async function requestGoogleToken(
  form: URLSearchParams,
  fetchFn: GoogleOAuthFetch,
): Promise<GoogleTokenResponse> {
  let payload: Record<string, unknown>;
  try {
    payload = await postGoogleTokenForm(form, fetchFn);
  } catch (error) {
    if (!(error instanceof GoogleTokenEndpointFailure)) {
      throw error;
    }
    throw new GmailOAuthError(
      "GOOGLE_TOKEN_EXCHANGE_FAILED",
      error.status === null
        ? "Google 授权响应无效，请重新授权"
        : "无法完成 Google 授权，请重新授权或稍后重试",
      { retryable: error.retryable, cause: error },
    );
  }
  const tokenType = readText(payload, "token_type");
  const expiresInSeconds = payload.expires_in;
  if (
    tokenType.toLowerCase() !== "bearer"
    || !Number.isSafeInteger(expiresInSeconds)
    || (expiresInSeconds as number) <= 0
  ) {
    throw malformedGoogleResponse("token");
  }

  return {
    accessToken: readText(payload, "access_token"),
    refreshToken: readText(payload, "refresh_token"),
    grantedScopeText: readText(payload, "scope"),
    expiresInSeconds: expiresInSeconds as number,
  };
}

/** OAuth code exchange 与后台 refresh 共用的 token HTTP/JSON 边界。 */
async function postGoogleTokenForm(
  form: URLSearchParams,
  fetchFn: GoogleOAuthFetch,
): Promise<Record<string, unknown>> {
  let response: Response;
  try {
    response = await fetchFn(GOOGLE_TOKEN_ENDPOINT, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: form,
      signal: AbortSignal.timeout(GOOGLE_OAUTH_TIMEOUT_MILLISECONDS),
    });
  } catch (error) {
    throw new GoogleTokenEndpointFailure(null, true, "Google token request failed", error);
  }
  if (!response.ok) {
    const oauthError = await readGoogleOAuthErrorCode(response);
    throw new GoogleTokenEndpointFailure(
      response.status,
      response.status === 429 || response.status >= 500,
      // 附上 Google 的稳定错误码（如 invalid_grant / redirect_uri_mismatch）：
      // 它只留在原因链里用于定位，页面仍只看到上层的收敛错误码。
      `Google token endpoint returned HTTP ${response.status}`
        + (oauthError === null ? "" : ` (${oauthError})`),
    );
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch (error) {
    throw new GoogleTokenEndpointFailure(null, false, "Google token response is malformed", error);
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    throw new GoogleTokenEndpointFailure(null, false, "Google token response is malformed");
  }
  return payload as Record<string, unknown>;
}

/**
 * 读取 Google 错误响应里的 OAuth 错误码。
 *
 * 只接受 OAuth 规范定义的短标识（小写字母与下划线），`error_description`
 * 等自由文本一律丢弃，避免把上游正文带进日志或终端。
 *
 * @param response - 非 2xx 的令牌端点响应。
 * @returns 合法错误码；读取失败或形状不符时为 null。
 */
async function readGoogleOAuthErrorCode(response: Response): Promise<string | null> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    return null;
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const error = (payload as Record<string, unknown>).error;
  return typeof error === "string" && /^[a-z_]{1,60}$/u.test(error) ? error : null;
}

/** 使用临时 access token 读取身份；未验证邮箱绝不建立连接。 */
async function requestVerifiedGoogleIdentity(
  accessToken: string,
  fetchFn: GoogleOAuthFetch,
): Promise<VerifiedGoogleIdentity> {
  let response: Response;
  try {
    response = await fetchFn(GOOGLE_USERINFO_ENDPOINT, {
      method: "GET",
      headers: { authorization: `Bearer ${accessToken}` },
      signal: AbortSignal.timeout(GOOGLE_OAUTH_TIMEOUT_MILLISECONDS),
    });
  } catch (error) {
    throw new GmailOAuthError(
      "GOOGLE_TOKEN_EXCHANGE_FAILED",
      "无法验证 Google 账户身份，请稍后重试",
      { retryable: true, cause: error },
    );
  }
  if (!response.ok) {
    throw googleEndpointError("userinfo", response.status);
  }

  const payload = await readJsonObject(response, "userinfo");
  const subject = readText(payload, "sub");
  const email = readText(payload, "email").toLowerCase();
  if (payload.email_verified !== true || !/^[^@\s]+@[^@\s]+$/.test(email)) {
    throw new GmailOAuthError(
      "GOOGLE_IDENTITY_UNVERIFIED",
      "Google 邮箱身份尚未验证",
    );
  }
  return { subject, email, emailVerified: true };
}

/** 解析 JSON object；不把 Google 原始正文放入异常。 */
async function readJsonObject(
  response: Response,
  endpoint: "token" | "userinfo",
): Promise<Record<string, unknown>> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch (error) {
    throw malformedGoogleResponse(endpoint, error);
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    throw malformedGoogleResponse(endpoint);
  }
  return payload as Record<string, unknown>;
}

/** 读取 Google 响应中的必需非空文本字段。 */
function readText(payload: Record<string, unknown>, field: string): string {
  const value = nonEmptyText(payload[field]);
  if (value === null) {
    throw malformedGoogleResponse(field === "sub" || field === "email" ? "userinfo" : "token");
  }
  return value;
}

/** HTTP 失败仅记录端点与状态，不读取可能含敏感信息的原始错误正文。 */
function googleEndpointError(
  endpoint: "token" | "userinfo",
  status: number,
): GmailOAuthError {
  return new GmailOAuthError(
    "GOOGLE_TOKEN_EXCHANGE_FAILED",
    endpoint === "token"
      ? "无法完成 Google 授权，请重新授权或稍后重试"
      : "无法验证 Google 账户身份，请重新授权或稍后重试",
    {
      retryable: status === 429 || status >= 500,
      cause: new Error(`Google ${endpoint} endpoint returned HTTP ${status}`),
    },
  );
}

/** 统一处理字段缺失、类型错误及非法 JSON。 */
function malformedGoogleResponse(
  endpoint: "token" | "userinfo",
  cause?: unknown,
): GmailOAuthError {
  return new GmailOAuthError(
    "GOOGLE_TOKEN_EXCHANGE_FAILED",
    "Google 授权响应无效，请重新授权",
    {
      cause: cause ?? new Error(`Google ${endpoint} response is malformed`),
    },
  );
}

/** 刷新成功响应缺少必需字段时，拒绝把部分结果交给 Gmail 客户端。 */
function invalidRefreshResponse(): GmailSyncError {
  return new GmailSyncError(
    "GMAIL_CONNECTION_ERROR",
    "Google 刷新授权响应无效，需要重新连接 Gmail",
  );
}

/** 读取未知值中的非空文本，不隐式字符串化。 */
function nonEmptyText(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : null;
}

/** 计算短期令牌到期时间，并拒绝超出 Date 可表达范围的响应。 */
function calculateAccessTokenExpiry(now: Date, expiresInSeconds: number): string | null {
  const expiresAt = new Date(now.getTime() + expiresInSeconds * 1_000);
  return Number.isNaN(expiresAt.getTime()) ? null : expiresAt.toISOString();
}

/** 连接 refresh token 的唯一解密入口，统一状态和密文错误语义。 */
function decryptConnectionRefreshToken(
  connection: GmailConnectionRecord,
  cipher: SecretCipher,
): string {
  if (connection.status === "disconnected" || connection.encryptedRefreshToken === null) {
    throw new GmailSyncError(
      "GMAIL_AUTH_REVOKED",
      "Gmail 连接已断开，需要重新授权",
    );
  }
  try {
    return cipher.decrypt(
      connection.encryptedRefreshToken,
      `gmail-connection:${connection.id}`,
    );
  } catch (error) {
    if (!(error instanceof SecretDecryptionError)) {
      throw error;
    }
    throw new GmailSyncError(
      "GMAIL_CONNECTION_ERROR",
      "存储的 Gmail 凭据无法解密，需要检查服务端密钥或重新连接",
      { cause: error },
    );
  }
}

/** 拒绝缺失的调用参数，避免向 Google 发送不完整授权请求。 */
function requireText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}

/** 校验 Android deep link、HTTPS callback 或本机 loopback redirect URI。 */
function requireRedirectUri(value: string): string {
  const normalized = requireText(value, "Google OAuth redirect URI");
  let url: URL;
  try {
    url = new URL(normalized);
  } catch (error) {
    throw new RangeError("Google OAuth redirect URI 无效", { cause: error });
  }
  if (url.username.length > 0 || url.password.length > 0 || url.hash.length > 0) {
    throw new RangeError("Google OAuth redirect URI 不能包含凭据或 fragment");
  }
  return normalized;
}

/** 校验可信时钟，避免生成无效 access token 到期时间。 */
function requireDate(value: Date): Date {
  if (Number.isNaN(value.getTime())) {
    throw new RangeError("Google 授权时间无效");
  }
  return value;
}
