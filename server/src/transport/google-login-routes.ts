import type { FastifyInstance } from "fastify";
import type { GoogleLoginService } from "../application/google-login-service.js";
import { OwnerLoginService } from "../application/owner-login-service.js";
import { registerLoginRoutes, type LoginRouteOptions } from "./login-routes.js";

export interface GoogleLoginRouteOptions extends Omit<LoginRouteOptions, "loginService"> {
  loginService: GoogleLoginService | null;
}

/** 兼容既有 Google 路由装配入口；生产入口统一注册三种登录。 */
export function registerGoogleLoginRoutes(app: FastifyInstance, options: GoogleLoginRouteOptions): void {
  registerLoginRoutes(app, {
    ...options,
    loginService: options.loginService?.ownerService ?? new OwnerLoginService({
      providers: [], devicePairingService: options.pairingService,
    }),
  });
}
