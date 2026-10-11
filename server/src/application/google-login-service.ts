import type { GoogleLoginConfig, GoogleLoginIdentity, GoogleLoginProvider } from "../infrastructure/auth/google-login-client.js";
import type { DevicePairingService } from "./device-pairing-service.js";
import { configuredGoogleLoginProvider } from "./owner-login-providers.js";
import {
  OwnerLoginService,
  type LoginStartInput,
  type LoginStartResponse,
  type LoginExchangeResponse,
} from "./owner-login-service.js";

export { pkceChallenge } from "./owner-login-service.js";
export type GoogleLoginStartInput = LoginStartInput;
export type GoogleLoginStartResponse = LoginStartResponse;
export type GoogleLoginExchangeResponse = { status: "pending" } | (
  Exclude<LoginExchangeResponse, { status: "pending" }> & { user: GoogleLoginIdentity }
);

/** 保留既有 Google 调用契约；所有登录事务统一由 OwnerLoginService 管理。 */
export class GoogleLoginService {
  readonly ownerService: OwnerLoginService;

  constructor(input: {
    config: GoogleLoginConfig;
    devicePairingService: Pick<DevicePairingService, "authorizeOwnerDevice">;
    googleClient?: GoogleLoginProvider;
    now?: () => Date;
  }) {
    this.ownerService = new OwnerLoginService({
      providers: [configuredGoogleLoginProvider(input.config, input.googleClient)],
      devicePairingService: input.devicePairingService,
      ...(input.now === undefined ? {} : { now: input.now }),
    });
  }

  start(input: GoogleLoginStartInput, sourceKey?: string): GoogleLoginStartResponse {
    return this.ownerService.start("google", input, sourceKey);
  }

  completeCallback(input: { state: string; code?: string; error?: string }): Promise<void> {
    return this.ownerService.completeCallback("google", input);
  }

  async exchange(input: { loginId: string; codeVerifier: string }): Promise<GoogleLoginExchangeResponse> {
    const result = await this.ownerService.exchange("google", input);
    if (result.status === "pending") return result;
    if (result.user.provider !== "google" || result.user.email === null) {
      throw new Error("Google 登录身份无效");
    }
    return { ...result, user: { ...result.user, provider: "google", email: result.user.email } };
  }
}
