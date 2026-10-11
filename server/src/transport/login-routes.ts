import type { FastifyInstance } from "fastify";
import { z } from "zod";

import { ApplicationError } from "../application/app-error.js";
import type { DevicePairingService } from "../application/device-pairing-service.js";
import type { OwnerLoginService } from "../application/owner-login-service.js";
import { LOGIN_PROVIDER_IDS, LOGIN_PROVIDER_NAMES, type LoginProviderId } from "../infrastructure/auth/login-provider.js";
import type { PairedDeviceAuthRecord } from "../infrastructure/security/device-auth.js";

export interface LoginRouteOptions {
  loginService: OwnerLoginService;
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
export function registerLoginRoutes(app: FastifyInstance, options: LoginRouteOptions): void {
  const service = options.loginService;
  app.get("/v1/auth/providers", async (_request, reply) => {
    reply.header("cache-control", "no-store");
    return service.catalog();
  });

  for (const provider of LOGIN_PROVIDER_IDS) {
    app.post(`/v1/auth/${provider}/start`, async (request, reply) => {
      reply.header("cache-control", "no-store");
      const body = startSchema.safeParse(request.body);
      if (!body.success) {
        throw invalidArgument();
      }
      // `/start` 无凭据：按来源限流，避免单一来源批量创建登录事务。
      return reply.code(201).send(service.start(provider, body.data, request.ip));
    });

    app.post(`/v1/auth/${provider}/exchange`, async (request, reply) => {
      reply.header("cache-control", "no-store");
      const body = exchangeSchema.safeParse(request.body);
      if (!body.success) {
        throw invalidArgument();
      }
      const result = await service.exchange(provider, body.data);
      return reply.code(result.status === "pending" ? 202 : 200).send(result);
    });

    app.get(`/v1/auth/${provider}/callback`, { logLevel: "silent" }, async (request, reply) => {
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
        await service.completeCallback(provider, {
          state: query.data.state,
          ...(query.data.code === undefined ? {} : { code: query.data.code }),
          ...(query.data.error === undefined ? {} : { error: query.data.error }),
        });
        return reply.type("text/html; charset=utf-8").send(callbackPage(true, provider));
      } catch (error: unknown) {
        const status = error instanceof ApplicationError ? error.statusCode : 401;
        return reply.code(status).type("text/html; charset=utf-8").send(callbackPage(false, provider));
      }
    });

  }

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

function callbackPage(success: boolean, provider: LoginProviderId): string {
  const title = success ? `${LOGIN_PROVIDER_NAMES[provider]} 验证完成` : "登录未完成";
  const message = success
    ? "请返回 Lyra App 完成登录。现在可以关闭此页面。"
    : "请返回 Lyra App 查看结果，或重新发起登录。";
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title} · Lyra</title></head><body><main><h1>${title}</h1><p>${message}</p></main></body></html>`;
}

function invalidArgument(): ApplicationError {
  return new ApplicationError({
    code: "INVALID_ARGUMENT", message: "登录参数无效", statusCode: 400, retryable: false,
  });
}
