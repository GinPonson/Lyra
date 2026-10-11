/** 登录目录固定顺序；来源与内部账户标识分离，不以邮箱或用户名自动关联身份。 */
export const LOGIN_PROVIDER_IDS = ["google", "linuxdo", "github"] as const;
export type LoginProviderId = typeof LOGIN_PROVIDER_IDS[number];

export const LOGIN_PROVIDER_NAMES: Readonly<Record<LoginProviderId, string>> = {
  google: "Google",
  linuxdo: "LINUX DO",
  github: "GitHub",
};

/** 只保留验证过的来源身份；第三方令牌永不进入 App 会话或持久化层。 */
export interface LoginIdentity {
  provider: LoginProviderId;
  subject: string;
  email: string | null;
  displayName: string | null;
}

/** Google OIDC 与普通 OAuth 共享登录事务，但由各自客户端验证上游身份。 */
export interface LoginProviderClient {
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
  }): Promise<LoginIdentity>;
}

/** 只有完整配置且绑定明确 owner 的来源才可进入可用登录目录。 */
export interface ConfiguredLoginProvider {
  id: LoginProviderId;
  name: string;
  client: LoginProviderClient;
  assertOwner: (identity: LoginIdentity) => void;
}
