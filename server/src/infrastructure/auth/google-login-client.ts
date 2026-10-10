import { createHash, timingSafeEqual } from "node:crypto";

import { OAuth2Client } from "google-auth-library";

import { ApplicationError } from "../../application/app-error.js";

const AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token";
export const GOOGLE_LOGIN_CALLBACK_PATH = "/v1/auth/google/callback";
export const GOOGLE_LOGIN_SCOPES = ["openid", "email", "profile"] as const;

/** 登录与 Gmail 离线授权使用不同配置；登录不申请 Gmail 或 offline 权限。 */
export interface GoogleLoginConfig {
  clientId: string;
  clientSecret: string;
  redirectUri: string;
  ownerEmail: string | null;
  ownerSubject: string | null;
}

/** 只接受经过 Google 签名与 OIDC 声明校验的身份，不持久化 Google 令牌。 */
export interface GoogleLoginIdentity {
  provider: "google";
  subject: string;
  email: string;
  displayName: string | null;
}

export interface GoogleLoginProvider {
  authorizationUrl(input: {
    state: string;
    nonce: string;
    codeChallenge: string;
  }): string;
  exchangeCode(input: {
    code: string;
    codeVerifier: string;
    nonceHash: string;
    now: Date;
  }): Promise<GoogleLoginIdentity>;
}

/** 无配置时保持未启用；部分配置或缺少明确 owner 时启动失败，绝不开放注册。 */
export function loadGoogleLoginConfig(
  env: Readonly<Record<string, string | undefined>>,
): GoogleLoginConfig | null {
  const names = [
    "LYRA_LOGIN_GOOGLE_CLIENT_ID",
    "LYRA_LOGIN_GOOGLE_CLIENT_SECRET",
    "LYRA_LOGIN_GOOGLE_REDIRECT_URI",
    "LYRA_OWNER_EMAIL",
    "LYRA_OWNER_GOOGLE_SUBJECT",
  ] as const;
  if (names.every((name) => (env[name]?.trim() ?? "") === "")) {
    return null;
  }
  const required = (name: typeof names[number]): string => {
    const value = env[name]?.trim();
    if (!value) {
      throw new RangeError(`Google 登录配置缺少 ${name}`);
    }
    return value;
  };
  const config: GoogleLoginConfig = {
    clientId: required("LYRA_LOGIN_GOOGLE_CLIENT_ID"),
    clientSecret: required("LYRA_LOGIN_GOOGLE_CLIENT_SECRET"),
    redirectUri: required("LYRA_LOGIN_GOOGLE_REDIRECT_URI"),
    ownerEmail: env.LYRA_OWNER_EMAIL?.trim().toLowerCase() || null,
    ownerSubject: env.LYRA_OWNER_GOOGLE_SUBJECT?.trim() || null,
  };
  validateGoogleLoginConfig(config);
  return config;
}

export function validateGoogleLoginConfig(config: GoogleLoginConfig): void {
  if (!config.clientId.trim() || !config.clientSecret.trim()) {
    throw new RangeError("Google 登录必须配置 Web OAuth client ID 与 client secret");
  }
  const redirect = new URL(config.redirectUri);
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(redirect.hostname);
  if (
    (redirect.protocol !== "https:" && !(redirect.protocol === "http:" && local))
    || redirect.pathname !== GOOGLE_LOGIN_CALLBACK_PATH
    || redirect.username || redirect.password || redirect.search || redirect.hash
  ) {
    throw new RangeError(`Google 登录回调必须为固定 HTTPS ${GOOGLE_LOGIN_CALLBACK_PATH} 地址（仅本机开发允许 HTTP）`);
  }
  if (config.ownerEmail === null && config.ownerSubject === null) {
    throw new RangeError("必须配置 LYRA_OWNER_EMAIL 或 LYRA_OWNER_GOOGLE_SUBJECT，Lyra 不开放注册");
  }
  if (config.ownerEmail !== null && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/u.test(config.ownerEmail)) {
    throw new RangeError("LYRA_OWNER_EMAIL 格式无效");
  }
  if (config.ownerSubject !== null && !/^[\x21-\x7e]{1,255}$/u.test(config.ownerSubject)) {
    throw new RangeError("LYRA_OWNER_GOOGLE_SUBJECT 格式无效");
  }
}

/** Google 官方库验证签名、issuer、audience、时效；此边界另外校验 nonce/邮箱。 */
export class GoogleLoginClient implements GoogleLoginProvider {
  readonly #config: GoogleLoginConfig;
  readonly #fetch: typeof globalThis.fetch;
  readonly #verifyIdToken: (idToken: string) => Promise<unknown>;

  constructor(
    config: GoogleLoginConfig,
    dependencies: {
      fetch?: typeof globalThis.fetch;
      verifyIdToken?: (idToken: string) => Promise<unknown>;
    } = {},
  ) {
    validateGoogleLoginConfig(config);
    this.#config = config;
    this.#fetch = dependencies.fetch ?? globalThis.fetch;
    const verifier = new OAuth2Client(config.clientId);
    this.#verifyIdToken = dependencies.verifyIdToken ?? (async (idToken) => (
      await verifier.verifyIdToken({ idToken, audience: config.clientId })
    ).getPayload());
  }

  authorizationUrl(input: { state: string; nonce: string; codeChallenge: string }): string {
    const url = new URL(AUTHORIZATION_ENDPOINT);
    for (const [name, value] of Object.entries({
      client_id: this.#config.clientId,
      redirect_uri: this.#config.redirectUri,
      response_type: "code",
      scope: GOOGLE_LOGIN_SCOPES.join(" "),
      state: input.state,
      nonce: input.nonce,
      code_challenge: input.codeChallenge,
      code_challenge_method: "S256",
      prompt: "select_account",
    })) {
      url.searchParams.set(name, value);
    }
    return url.toString();
  }

  async exchangeCode(input: {
    code: string;
    codeVerifier: string;
    nonceHash: string;
    now: Date;
  }): Promise<GoogleLoginIdentity> {
    try {
      const response = await this.#fetch(TOKEN_ENDPOINT, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          grant_type: "authorization_code",
          code: input.code,
          code_verifier: input.codeVerifier,
          client_id: this.#config.clientId,
          client_secret: this.#config.clientSecret,
          redirect_uri: this.#config.redirectUri,
        }),
        signal: AbortSignal.timeout(15_000),
      });
      if (!response.ok) {
        throw loginFailed();
      }
      const body: unknown = await response.json();
      if (!isRecord(body) || typeof body.id_token !== "string" || body.id_token.length === 0) {
        throw loginFailed();
      }
      const claims = await this.#verifyIdToken(body.id_token);
      return validateGoogleLoginClaims(claims, {
        audience: this.#config.clientId,
        nonceHash: input.nonceHash,
        now: input.now,
      });
    } catch {
      // Google 响应、授权码、client secret、ID token 和底层错误都不写入日志/响应。
      throw loginFailed();
    }
  }
}

/** 只用于签名已经通过的 payload；不能用 JSON 解码替代上面的官方验签入口。 */
export function validateGoogleLoginClaims(
  claims: unknown,
  expected: { audience: string; nonceHash: string; now: Date },
): GoogleLoginIdentity {
  const nowSeconds = expected.now.getTime() / 1_000;
  if (
    !isRecord(claims)
    || (claims.iss !== "https://accounts.google.com" && claims.iss !== "accounts.google.com")
    || claims.aud !== expected.audience
    || (claims.azp !== undefined && claims.azp !== expected.audience)
    || typeof claims.exp !== "number" || !Number.isSafeInteger(claims.exp) || claims.exp <= nowSeconds
    || typeof claims.iat !== "number" || !Number.isSafeInteger(claims.iat) || claims.iat > nowSeconds + 60
    || claims.exp <= claims.iat
    || typeof claims.nonce !== "string" || !matchesHash(claims.nonce, expected.nonceHash)
    || typeof claims.sub !== "string" || !/^[\x21-\x7e]{1,255}$/u.test(claims.sub)
    || claims.email_verified !== true
    || typeof claims.email !== "string" || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/u.test(claims.email)
  ) {
    throw loginFailed();
  }
  return {
    provider: "google",
    subject: claims.sub,
    email: claims.email.trim().toLowerCase(),
    displayName: typeof claims.name === "string" && claims.name.trim()
      ? claims.name.trim().slice(0, 256)
      : null,
  };
}

export function assertConfiguredOwner(identity: GoogleLoginIdentity, config: GoogleLoginConfig): void {
  if (
    (config.ownerSubject !== null && identity.subject !== config.ownerSubject)
    || (config.ownerEmail !== null && identity.email !== config.ownerEmail.toLowerCase())
  ) {
    throw new ApplicationError({
      code: "LOGIN_DENIED", message: "此 Google 账户无权访问这台 Lyra 服务", statusCode: 403, retryable: false,
    });
  }
}

export function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function matchesHash(value: string, hash: string): boolean {
  return /^[a-f\d]{64}$/u.test(hash)
    && timingSafeEqual(Buffer.from(sha256(value), "hex"), Buffer.from(hash, "hex"));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function loginFailed(): ApplicationError {
  return new ApplicationError({
    code: "LOGIN_FAILED", message: "无法验证 Google 登录，请重新登录", statusCode: 401, retryable: false,
  });
}
