import type { FastifyInstance } from "fastify";
import { z } from "zod";

import { ApplicationError } from "../application/app-error.js";
import type { DevicePairingService } from "../application/device-pairing-service.js";
import type { GoogleLoginService } from "../application/google-login-service.js";
import { GOOGLE_LOGIN_CALLBACK_PATH } from "../infrastructure/auth/google-login-client.js";
import type { PairedDeviceAuthRecord } from "../infrastructure/security/device-auth.js";

export interface GoogleLoginRouteOptions {
  loginService: GoogleLoginService | null;
  pairingService: DevicePairingService;
  authenticateDevice: (token: string) => Promise<PairedDeviceAuthRecord>;
  onLogout?: (deviceId: string) => void | Promise<void>;
}

const startSchema = z.object({
  displayName: z.string().trim().min(1).max(64),
  publicKeyPem: z.string().trim().min(1).max(4_096),
  codeChallenge: z.string().regex(/^[A-Za-z\d_-]{43}$/u),
  codeChallengeMethod: z.literal("S256"),
}).strict();

const exchangeSchema = z.object({
  loginId: z.string().uuid(),
  codeVerifier: z.string().regex(/^[A-Za-z\d._~-]{43,128}$/u),
}).strict();

const callbackSchema = z.object({
  state: z.string().min(1).max(256),
  code: z.string().min(1).max(4_096).optional(),
  error: z.string().min(1).max(256).optional(),
});

/** 浏览器仅完成身份验证；Lyra 凭据始终经 verifier 绑定的 POST 返回 App。 */
export function registerGoogleLoginRoutes(app: FastifyInstance, options: GoogleLoginRouteOptions): void {
  const service = (): GoogleLoginService => {
    if (options.loginService === null) {
      throw new ApplicationError({
        code: "AUTH_NOT_CONFIGURED", message: "服务端尚未配置 Google 登录", statusCode: 503, retryable: false,
      });
    }
    return options.loginService;
  };

  app.post("/v1/auth/google/start", async (request, reply) => {
    reply.header("cache-control", "no-store");
    const body = startSchema.safeParse(request.body);
    if (!body.success) {
      throw invalidArgument();
    }
    return reply.code(201).send(service().start(body.data));
  });

  app.post("/v1/auth/google/exchange", async (request, reply) => {
    reply.header("cache-control", "no-store");
    const body = exchangeSchema.safeParse(request.body);
    if (!body.success) {
      throw invalidArgument();
    }
    const result = await service().exchange(body.data);
    return reply.code(result.status === "pending" ? 202 : 200).send(result);
  });

  app.get(GOOGLE_LOGIN_CALLBACK_PATH, { logLevel: "silent" }, async (request, reply) => {
    reply.headers({
      "cache-control": "no-store",
      "pragma": "no-cache",
      "referrer-policy": "no-referrer",
      "content-security-policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
      "x-content-type-options": "nosniff",
    });
    try {
      const query = callbackSchema.safeParse(request.query);
      if (!query.success) {
        throw invalidArgument();
      }
      await service().completeCallback({
        state: query.data.state,
        ...(query.data.code === undefined ? {} : { code: query.data.code }),
        ...(query.data.error === undefined ? {} : { error: query.data.error }),
      });
      return reply.type("text/html; charset=utf-8").send(callbackPage(true));
    } catch (error: unknown) {
      const status = error instanceof ApplicationError ? error.statusCode : 401;
      return reply.code(status).type("text/html; charset=utf-8").send(callbackPage(false));
    }
  });

  app.post("/v1/auth/logout", async (request, reply) => {
    reply.header("cache-control", "no-store");
    const authorization = request.headers.authorization;
    if (typeof authorization !== "string" || !authorization.startsWith("Bearer ")) {
      throw new ApplicationError({
        code: "DEVICE_CREDENTIAL_INVALID", message: "设备访问令牌无效", statusCode: 401, retryable: false,
      });
    }
    const device = await options.authenticateDevice(authorization.slice("Bearer ".length));
    await options.pairingService.revokeDevice(device.id);
    await options.onLogout?.(device.id);
    return reply.code(204).send();
  });
}

function callbackPage(success: boolean): string {
  const title = success ? "Google 验证完成" : "登录未完成";
  const message = success
    ? "请返回 Lyra App 完成登录。现在可以关闭此页面。"
    : "请返回 Lyra App 查看结果，或重新发起 Google 登录。";
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title} · Lyra</title></head><body><main><h1>${title}</h1><p>${message}</p></main></body></html>`;
}

function invalidArgument(): ApplicationError {
  return new ApplicationError({
    code: "INVALID_ARGUMENT", message: "登录参数无效", statusCode: 400, retryable: false,
  });
}
