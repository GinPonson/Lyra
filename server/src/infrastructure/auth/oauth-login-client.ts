import { ApplicationError } from "../../application/app-error.js";
import type { LoginIdentity, LoginProviderClient } from "./login-provider.js";

export const GITHUB_LOGIN_CALLBACK_PATH = "/v1/auth/github/callback";
export const LINUXDO_LOGIN_CALLBACK_PATH = "/v1/auth/linuxdo/callback";

const MAX_RESPONSE_BYTES = 64 * 1_024;
const EXCHANGE_TIMEOUT_MS = 15_000;
const PROVIDERS = {
  github: {
    name: "GitHub",
    environmentName: "GITHUB",
    callbackPath: GITHUB_LOGIN_CALLBACK_PATH,
    authorizationEndpoint: "https://github.com/login/oauth/authorize",
    tokenEndpoint: "https://github.com/login/oauth/access_token",
    userEndpoint: "https://api.github.com/user",
    scope: "read:user",
  },
  linuxdo: {
    name: "LINUX DO",
    environmentName: "LINUXDO",
    callbackPath: LINUXDO_LOGIN_CALLBACK_PATH,
    authorizationEndpoint: "https://connect.linux.do/oauth2/authorize",
    tokenEndpoint: "https://connect.linux.do/oauth2/token",
    userEndpoint: "https://connect.linux.do/api/user",
    scope: "user",
  },
} as const;

/** 凭据只在服务端配置；每种来源都必须独立指定不可变的数字 owner ID。 */
export interface OAuthLoginConfig {
  provider: "github" | "linuxdo";
  clientId: string;
  clientSecret: string;
  redirectUri: string;
  ownerSubject: string;
}

/** 完全未配置时关闭来源；不接受部分配置，也不把用户名或邮箱作为 owner。 */
export function loadOAuthLoginConfig(
  env: Readonly<Record<string, string | undefined>>,
  provider: OAuthLoginConfig["provider"],
): OAuthLoginConfig | null {
  const definition = providerDefinition(provider);
  const prefix = `LYRA_LOGIN_${definition.environmentName}`;
  const ownerName = `LYRA_OWNER_${definition.environmentName}_SUBJECT`;
  const names = [`${prefix}_CLIENT_ID`, `${prefix}_CLIENT_SECRET`, `${prefix}_REDIRECT_URI`, ownerName];
  if (names.every((name) => (env[name]?.trim() ?? "") === "")) {
    return null;
  }
  const required = (name: string): string => {
    const value = env[name]?.trim();
    if (!value) {
      throw new RangeError(`${definition.name} 登录配置缺少 ${name}`);
    }
    return value;
  };
  const config: OAuthLoginConfig = {
    provider,
    clientId: required(`${prefix}_CLIENT_ID`),
    clientSecret: required(`${prefix}_CLIENT_SECRET`),
    redirectUri: required(`${prefix}_REDIRECT_URI`),
    ownerSubject: required(ownerName),
  };
  validateOAuthLoginConfig(config);
  return config;
}

export function validateOAuthLoginConfig(config: OAuthLoginConfig): void {
  const definition = providerDefinition(config.provider);
  for (const credential of [config.clientId, config.clientSecret]) {
    if (
      typeof credential !== "string" || !credential.trim() || credential.length > 4_096
      || /[\u0000-\u001f\u007f]/u.test(credential)
    ) {
      throw new RangeError(`${definition.name} 登录必须配置有效的 OAuth client ID 与 client secret`);
    }
  }
  const invalidRedirect = (): RangeError => new RangeError(
    `${definition.name} 登录回调必须为固定 HTTPS ${definition.callbackPath} 地址（仅本机开发允许 HTTP）`,
  );
  let redirect: URL;
  try {
    redirect = new URL(config.redirectUri);
  } catch {
    // URL 解析错误可能携带原始输入；不把含凭据的配置原文带到启动日志。
    throw invalidRedirect();
  }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(redirect.hostname);
  if (
    (redirect.protocol !== "https:" && !(redirect.protocol === "http:" && local))
    || redirect.pathname !== definition.callbackPath
    || redirect.username || redirect.password || redirect.search || redirect.hash
    || /[\u0000-\u0020\u007f?#]/u.test(config.redirectUri)
  ) {
    throw invalidRedirect();
  }
  if (!isCanonicalSubject(config.ownerSubject)) {
    throw new RangeError(
      `LYRA_OWNER_${definition.environmentName}_SUBJECT 必须是明确配置的正整数用户 ID，Lyra 不开放注册`,
    );
  }
}

/** 只认 provider + 稳定 ID；同名、同邮箱以及另一来源的相同 ID 都不会获得权限。 */
export function assertOAuthConfiguredOwner(identity: LoginIdentity, config: OAuthLoginConfig): void {
  if (identity.provider !== config.provider || identity.subject !== config.ownerSubject) {
    throw new ApplicationError({
      code: "LOGIN_DENIED",
      message: `此 ${providerDefinition(config.provider).name} 账户无权访问这台 Lyra 服务`,
      statusCode: 403,
      retryable: false,
    });
  }
}

/**
 * 普通 OAuth 不将 access token 当作可自行解码的身份声明：每次交换后向固定用户端点验证。
 * GitHub 使用 S256 PKCE；LINUX DO 尚无官方 PKCE 支持保证，使用服务端 confidential client。
 * App 到 Lyra 的独立 PKCE 和 state/事务绑定仍由登录服务负责。
 */
export class OAuthLoginClient implements LoginProviderClient {
  readonly #config: Readonly<OAuthLoginConfig>;
  readonly #fetch: typeof globalThis.fetch;

  constructor(
    config: OAuthLoginConfig,
    dependencies: { fetch?: typeof globalThis.fetch } = {},
  ) {
    validateOAuthLoginConfig(config);
    this.#config = Object.freeze({ ...config });
    this.#fetch = dependencies.fetch ?? globalThis.fetch;
  }

  authorizationUrl(input: { state: string; nonce: string; codeChallenge: string }): string {
    const definition = providerDefinition(this.#config.provider);
    const url = new URL(definition.authorizationEndpoint);
    for (const [name, value] of Object.entries({
      client_id: this.#config.clientId,
      redirect_uri: this.#config.redirectUri,
      response_type: "code",
      scope: definition.scope,
      state: input.state,
    })) {
      url.searchParams.set(name, value);
    }
    if (this.#config.provider === "github") {
      url.searchParams.set("code_challenge", input.codeChallenge);
      url.searchParams.set("code_challenge_method", "S256");
    }
    return url.toString();
  }

  async exchangeCode(input: {
    code: string;
    codeVerifier: string;
    nonceHash: string;
    now: Date;
  }): Promise<LoginIdentity> {
    const definition = providerDefinition(this.#config.provider);
    try {
      // 同一次交换的 token 和用户请求共用期限，避免串行请求扩大超时时间。
      const signal = AbortSignal.timeout(EXCHANGE_TIMEOUT_MS);
      const form = new URLSearchParams({
        grant_type: "authorization_code",
        code: input.code,
        client_id: this.#config.clientId,
        client_secret: this.#config.clientSecret,
        redirect_uri: this.#config.redirectUri,
      });
      if (this.#config.provider === "github") {
        form.set("code_verifier", input.codeVerifier);
      }
      const token = await readBoundedJson(await this.#fetch(definition.tokenEndpoint, {
        method: "POST",
        headers: {
          "content-type": "application/x-www-form-urlencoded",
          accept: "application/json",
          "user-agent": "Lyra",
        },
        body: form,
        redirect: "error",
        credentials: "omit",
        cache: "no-store",
        signal,
      }));
      if (
        !isRecord(token) || token.error !== undefined
        || typeof token.access_token !== "string"
        || !/^[A-Za-z\d._~+/-]{1,8192}={0,2}$/u.test(token.access_token)
        || typeof token.token_type !== "string" || token.token_type.toLowerCase() !== "bearer"
      ) {
        throw new Error("Invalid OAuth token response");
      }
      const headers: Record<string, string> = {
        authorization: `Bearer ${token.access_token}`,
        accept: this.#config.provider === "github" ? "application/vnd.github+json" : "application/json",
        "user-agent": "Lyra",
      };
      if (this.#config.provider === "github") {
        headers["x-github-api-version"] = "2026-03-10";
      }
      const profile = await readBoundedJson(await this.#fetch(definition.userEndpoint, {
        method: "GET",
        headers,
        redirect: "error",
        credentials: "omit",
        cache: "no-store",
        signal,
      }));
      return identityFromProfile(profile, this.#config.provider);
    } catch {
      // 不传播底层异常/cause/响应正文：其中可能包含授权码、secret 或第三方令牌。
      throw new ApplicationError({
        code: "LOGIN_FAILED",
        message: `无法验证 ${definition.name} 登录，请重新登录`,
        statusCode: 401,
        retryable: false,
      });
    }
  }
}

function providerDefinition(provider: OAuthLoginConfig["provider"]): typeof PROVIDERS[keyof typeof PROVIDERS] {
  if (provider !== "github" && provider !== "linuxdo") {
    throw new RangeError("不支持此 OAuth 登录来源");
  }
  return PROVIDERS[provider];
}

function isCanonicalSubject(value: unknown): value is string {
  return typeof value === "string" && /^[1-9]\d{0,15}$/u.test(value)
    && Number.isSafeInteger(Number(value)) && String(Number(value)) === value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function identityFromProfile(profile: unknown, provider: OAuthLoginConfig["provider"]): LoginIdentity {
  if (
    !isRecord(profile) || profile.error !== undefined
    || typeof profile.id !== "number" || !Number.isSafeInteger(profile.id) || profile.id <= 0
  ) {
    throw new Error("Invalid OAuth user identity");
  }
  if (provider === "linuxdo") {
    if (
      (profile.active !== undefined && typeof profile.active !== "boolean")
      || profile.active === false
      || (profile.silenced !== undefined && typeof profile.silenced !== "boolean")
    ) {
      throw new Error("Invalid OAuth account status");
    }
    // silenced 是论坛禁言状态，不代表第三方登录被禁用；owner 准入只看不可变 ID。
  }
  const fallback = provider === "github" ? profile.login : profile.username;
  return {
    provider,
    subject: String(profile.id),
    // 只做身份登录，不申请或推断经验证邮箱，也不把额外 profile 字段透传出去。
    email: null,
    displayName: displayName(profile.name) ?? displayName(fallback),
  };
}

function displayName(value: unknown): string | null {
  return typeof value === "string" && value.trim()
    ? value.replace(/[\u0000-\u001f\u007f]/gu, "").trim().slice(0, 256) || null
    : null;
}

/** 按真实字节计数，不能仅信任 Content-Length 或先无上限地 response.json()。 */
async function readBoundedJson(response: Response): Promise<unknown> {
  const declaredLength = response.headers.get("content-length");
  if (
    !response.ok || response.redirected || !response.body
    || (declaredLength !== null && (!/^\d+$/u.test(declaredLength) || Number(declaredLength) > MAX_RESPONSE_BYTES))
  ) {
    await response.body?.cancel().catch(() => undefined);
    throw new Error("Invalid OAuth HTTP response");
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      length += chunk.value.byteLength;
      if (length > MAX_RESPONSE_BYTES) {
        throw new Error("OAuth response is too large");
      }
      chunks.push(chunk.value);
    }
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks, length))) as unknown;
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
