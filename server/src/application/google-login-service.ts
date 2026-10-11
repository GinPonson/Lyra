import { createHash, randomBytes, randomUUID, timingSafeEqual } from "node:crypto";

import {
  GoogleLoginClient,
  assertConfiguredOwner,
  sha256,
  validateGoogleLoginConfig,
  type GoogleLoginConfig,
  type GoogleLoginIdentity,
  type GoogleLoginProvider,
} from "../infrastructure/auth/google-login-client.js";
import { DeviceAuthError, normalizeDevicePublicKey } from "../infrastructure/security/device-auth.js";
import { ApplicationError } from "./app-error.js";
import type { DevicePairingHttpResponse, DevicePairingService } from "./device-pairing-service.js";

const LOGIN_TTL_MILLISECONDS = 10 * 60 * 1_000;
/** 终态事务的保留时长：短暂保留可让重放得到准确的 409/410，随后立即释放容量。 */
const TERMINAL_RETENTION_MILLISECONDS = 60 * 1_000;
const MAX_LOGIN_ATTEMPTS = 128;
/** 单个来源的滚动窗口限流；`/start` 无凭据，必须防止单点占满全局容量。 */
const SOURCE_WINDOW_MILLISECONDS = 60 * 1_000;
const MAX_STARTS_PER_SOURCE_WINDOW = 20;

export interface GoogleLoginStartInput {
  displayName: string;
  publicKeyPem: string;
  codeChallenge: string;
  codeChallengeMethod: "S256";
}

export interface GoogleLoginStartResponse {
  loginId: string;
  authorizationUrl: string;
  expiresAt: string;
  pollIntervalSeconds: number;
}

export type GoogleLoginExchangeResponse = { status: "pending" } | (
  DevicePairingHttpResponse & { status: "complete"; user: GoogleLoginIdentity }
);

interface LoginAttempt {
  id: string;
  stateHash: string;
  nonceHash: string;
  googleCodeVerifier: string | null;
  appCodeChallenge: string;
  publicKeyPem: string;
  displayName: string;
  expiresAt: number;
  /** 进入 consumed/failed 的时间；null 表示仍在进行中。 */
  terminatedAt: number | null;
  status: "pending" | "verifying" | "approved" | "exchanging" | "consumed" | "failed";
  identity: GoogleLoginIdentity | null;
  error: ApplicationError | null;
}

/**
 * 两层独立 PKCE：服务端→Google 的授权码交换，以及 App→Lyra 的凭据兑换。
 * App verifier 不进入浏览器。回调只更新内存状态，最终凭据仅由持有 verifier
 * 的发起安装经 POST 领取；事务一次性且重启即失效。
 */
export class GoogleLoginService {
  readonly #config: GoogleLoginConfig;
  readonly #devices: Pick<DevicePairingService, "authorizeOwnerDevice">;
  readonly #google: GoogleLoginProvider;
  readonly #now: () => Date;
  readonly #attempts = new Map<string, LoginAttempt>();
  readonly #states = new Map<string, string>();
  /** 每个来源在滚动窗口内的 /start 时间戳，用于无凭据端点的限流。 */
  readonly #sourceStarts = new Map<string, number[]>();

  constructor(input: {
    config: GoogleLoginConfig;
    devicePairingService: Pick<DevicePairingService, "authorizeOwnerDevice">;
    googleClient?: GoogleLoginProvider;
    now?: () => Date;
  }) {
    validateGoogleLoginConfig(input.config);
    this.#config = input.config;
    this.#devices = input.devicePairingService;
    this.#google = input.googleClient ?? new GoogleLoginClient(input.config);
    this.#now = input.now ?? (() => new Date());
  }

  start(input: GoogleLoginStartInput, sourceKey?: string): GoogleLoginStartResponse {
    if (
      input.codeChallengeMethod !== "S256"
      || !/^[A-Za-z\d_-]{43}$/u.test(input.codeChallenge)
      || input.displayName.trim().length < 1 || input.displayName.trim().length > 64
      || input.publicKeyPem.length > 4_096
    ) {
      throw new ApplicationError({
        code: "INVALID_ARGUMENT", message: "登录参数无效", statusCode: 400, retryable: false,
      });
    }
    let publicKeyPem: string;
    try {
      publicKeyPem = normalizeDevicePublicKey(input.publicKeyPem).pem;
    } catch (error: unknown) {
      if (!(error instanceof DeviceAuthError)) {
        throw error;
      }
      throw new ApplicationError({
        code: "DEVICE_PUBLIC_KEY_INVALID", message: "设备公钥格式无效", statusCode: 400, retryable: false,
      });
    }
    this.#pruneExpired();
    this.#assertSourceBudget(sourceKey);
    // 容量被滥用时淘汰最早的事务，而不是对所有人返回 429：否则未认证的
    // 攻击者只要占满 128 个槽位，就能让所有者永久无法发起新登录。
    // 淘汰顺序按 Map 的插入顺序（最早优先），最新请求始终可以继续。
    while (this.#attempts.size >= MAX_LOGIN_ATTEMPTS) {
      const oldest = this.#attempts.keys().next().value;
      if (oldest === undefined) {
        break;
      }
      this.#dropAttempt(oldest);
    }
    const now = this.#now();
    const state = randomBytes(32).toString("base64url");
    const nonce = randomBytes(32).toString("base64url");
    const googleCodeVerifier = randomBytes(48).toString("base64url");
    const attempt: LoginAttempt = {
      id: randomUUID(),
      stateHash: sha256(state),
      nonceHash: sha256(nonce),
      googleCodeVerifier,
      appCodeChallenge: input.codeChallenge,
      publicKeyPem,
      displayName: input.displayName.trim(),
      expiresAt: now.getTime() + LOGIN_TTL_MILLISECONDS,
      terminatedAt: null,
      status: "pending",
      identity: null,
      error: null,
    };
    const authorizationUrl = this.#google.authorizationUrl({
      state, nonce, codeChallenge: pkceChallenge(googleCodeVerifier),
    });
    this.#attempts.set(attempt.id, attempt);
    this.#states.set(attempt.stateHash, attempt.id);
    return {
      loginId: attempt.id,
      authorizationUrl,
      expiresAt: new Date(attempt.expiresAt).toISOString(),
      pollIntervalSeconds: 2,
    };
  }

  /** public browser callback；在首次 await 前占用状态，阻止授权码并发消费。 */
  async completeCallback(input: { state: string; code?: string; error?: string }): Promise<void> {
    const loginId = this.#states.get(sha256(input.state));
    if (!/^[A-Za-z\d_-]{43}$/u.test(input.state) || loginId === undefined) {
      throw new ApplicationError({
        code: "LOGIN_STATE_INVALID", message: "登录请求已失效，请返回 App 重新登录", statusCode: 400, retryable: false,
      });
    }
    const attempt = this.#requireAttempt(loginId);
    if (attempt.status !== "pending") {
      throw alreadyRedeemed();
    }
    attempt.status = "verifying";
    const codeVerifier = attempt.googleCodeVerifier;
    attempt.googleCodeVerifier = null;
    try {
      if (input.error !== undefined) {
        throw input.error === "access_denied"
          ? new ApplicationError({ code: "LOGIN_DENIED", message: "Google 登录已取消", statusCode: 403, retryable: false })
          : loginFailed();
      }
      if (!input.code || input.code.length > 4_096 || codeVerifier === null) {
        throw loginFailed();
      }
      const identity = await this.#google.exchangeCode({
        code: input.code,
        codeVerifier,
        nonceHash: attempt.nonceHash,
        now: this.#now(),
      });
      this.#assertNotExpired(attempt);
      assertConfiguredOwner(identity, this.#config);
      attempt.identity = identity;
      attempt.status = "approved";
    } catch (error: unknown) {
      attempt.status = "failed";
      attempt.terminatedAt = this.#now().getTime();
      attempt.error = error instanceof ApplicationError ? error : loginFailed();
      throw attempt.error;
    }
  }

  /** 轮询与最终兑换都需要 App verifier；loginId 或浏览器 URL 均不足以领取凭据。 */
  async exchange(input: { loginId: string; codeVerifier: string }): Promise<GoogleLoginExchangeResponse> {
    const attempt = this.#requireAttempt(input.loginId);
    if (
      !/^[A-Za-z\d._~-]{43,128}$/u.test(input.codeVerifier)
      || !timingSafeEqual(
        Buffer.from(pkceChallenge(input.codeVerifier)), Buffer.from(attempt.appCodeChallenge),
      )
    ) {
      throw loginFailed();
    }
    if (attempt.status === "pending" || attempt.status === "verifying") {
      return { status: "pending" };
    }
    if (attempt.status === "failed") {
      throw attempt.error ?? loginFailed();
    }
    if (attempt.status !== "approved" || attempt.identity === null) {
      throw alreadyRedeemed();
    }
    // 先占用，再开始数据库事务；同一事件循环中的并发兑换只能有一个胜者。
    attempt.status = "exchanging";
    const identity = attempt.identity;
    try {
      const credentials = await this.#devices.authorizeOwnerDevice({
        identity,
        publicKeyPem: attempt.publicKeyPem,
        displayName: attempt.displayName,
      });
      attempt.status = "consumed";
      attempt.terminatedAt = this.#now().getTime();
      attempt.identity = null;
      return { status: "complete", ...credentials, user: identity };
    } catch (error: unknown) {
      attempt.status = "failed";
      attempt.terminatedAt = this.#now().getTime();
      attempt.identity = null;
      attempt.error = error instanceof ApplicationError ? error : loginFailed();
      throw attempt.error;
    }
  }

  #requireAttempt(id: string): LoginAttempt {
    const attempt = this.#attempts.get(id);
    if (attempt === undefined) {
      throw loginExpired();
    }
    this.#assertNotExpired(attempt);
    return attempt;
  }

  #assertNotExpired(attempt: LoginAttempt): void {
    if (this.#now().getTime() >= attempt.expiresAt) {
      throw loginExpired();
    }
  }

  #pruneExpired(): void {
    const now = this.#now().getTime();
    for (const attempt of this.#attempts.values()) {
      const expired = now >= attempt.expiresAt;
      // 终态事务只短暂保留，否则一次正常登录会占用全局容量直到 TTL 结束。
      const finished = attempt.terminatedAt !== null
        && now >= attempt.terminatedAt + TERMINAL_RETENTION_MILLISECONDS;
      if (expired || finished) {
        this.#dropAttempt(attempt.id);
      }
    }
    // 顺手清空已无时间戳的来源窗口，避免限流表随来源数无限增长。
    for (const [source, timestamps] of this.#sourceStarts) {
      const recent = timestamps.filter((at) => now - at < SOURCE_WINDOW_MILLISECONDS);
      if (recent.length === 0) {
        this.#sourceStarts.delete(source);
      } else if (recent.length !== timestamps.length) {
        this.#sourceStarts.set(source, recent);
      }
    }
  }

  /** 释放一个事务：同时移除登录 ID 与 state 哈希两个索引，避免残留。 */
  #dropAttempt(loginId: string): void {
    const attempt = this.#attempts.get(loginId);
    if (attempt === undefined) {
      return;
    }
    this.#attempts.delete(loginId);
    this.#states.delete(attempt.stateHash);
  }

  /**
   * 无凭据端点的单来源滚动窗口限流。
   *
   * `/start` 生成 state/nonce 与 Google 授权 URL，必须防止单一来源在窗口内
   * 批量创建事务（容量已改为淘汰最早事务，限流用于压制攻击节奏）。
   */
  #assertSourceBudget(sourceKey: string | undefined): void {
    if (sourceKey === undefined || sourceKey.length === 0) {
      return;
    }
    const now = this.#now().getTime();
    const recent = (this.#sourceStarts.get(sourceKey) ?? [])
      .filter((at) => now - at < SOURCE_WINDOW_MILLISECONDS);
    if (recent.length >= MAX_STARTS_PER_SOURCE_WINDOW) {
      throw new ApplicationError({
        code: "LOGIN_RATE_LIMITED", message: "登录请求过多，请稍后重试", statusCode: 429, retryable: true,
      });
    }
    recent.push(now);
    this.#sourceStarts.set(sourceKey, recent);
  }
}

export function pkceChallenge(verifier: string): string {
  return createHash("sha256").update(verifier, "ascii").digest("base64url");
}

function alreadyRedeemed(): ApplicationError {
  return new ApplicationError({
    code: "LOGIN_ALREADY_REDEEMED", message: "此登录请求已经处理，请重新登录", statusCode: 409, retryable: false,
  });
}

function loginFailed(): ApplicationError {
  return new ApplicationError({
    code: "LOGIN_FAILED", message: "无法完成登录，请重新登录", statusCode: 401, retryable: false,
  });
}

function loginExpired(): ApplicationError {
  return new ApplicationError({
    code: "LOGIN_EXPIRED", message: "登录请求已过期，请重新登录", statusCode: 410, retryable: false,
  });
}
