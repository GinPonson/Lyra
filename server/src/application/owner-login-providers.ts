import { ApplicationError } from "./app-error.js";
import {
  GoogleLoginClient,
  assertConfiguredOwner,
  loadGoogleLoginConfig,
  validateGoogleLoginConfig,
  type GoogleLoginConfig,
  type GoogleLoginProvider,
} from "../infrastructure/auth/google-login-client.js";
import {
  OAuthLoginClient,
  assertOAuthConfiguredOwner,
  loadOAuthLoginConfig,
} from "../infrastructure/auth/oauth-login-client.js";
import type { ConfiguredLoginProvider } from "../infrastructure/auth/login-provider.js";

export function configuredGoogleLoginProvider(
  config: GoogleLoginConfig,
  client?: GoogleLoginProvider,
): ConfiguredLoginProvider {
  validateGoogleLoginConfig(config);
  return {
    id: "google",
    name: "Google",
    client: client ?? new GoogleLoginClient(config),
    assertOwner(identity) {
      if (identity.provider !== "google" || identity.email === null) {
        throw new ApplicationError({
          code: "LOGIN_FAILED", message: "无法确认登录身份", statusCode: 401, retryable: false,
        });
      }
      assertConfiguredOwner({ ...identity, provider: "google", email: identity.email }, config);
    },
  };
}

/** 先完整验证所有已填写的提供方配置；任何部分配置都在数据库迁移前失败。 */
export function loadOwnerLoginProviders(
  environment: Readonly<Record<string, string | undefined>>,
): ConfiguredLoginProvider[] {
  const google = loadGoogleLoginConfig(environment);
  const others = (["linuxdo", "github"] as const)
    .map((provider) => loadOAuthLoginConfig(environment, provider));
  const providers: ConfiguredLoginProvider[] = [];
  if (google !== null) providers.push(configuredGoogleLoginProvider(google));
  for (const config of others) {
    if (config === null) continue;
    providers.push({
      id: config.provider,
      name: config.provider === "github" ? "GitHub" : "LINUX DO",
      client: new OAuthLoginClient(config),
      assertOwner: (identity) => assertOAuthConfiguredOwner(identity, config),
    });
  }
  return providers;
}
