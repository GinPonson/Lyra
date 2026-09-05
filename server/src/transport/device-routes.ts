import type { FastifyInstance } from "fastify";
import { z } from "zod";

import type { DevicePairingService } from "../application/index.js";
import { ApplicationError } from "../application/index.js";

/** 设备配对与令牌签发路由的装配依赖。 */
export interface DeviceRouteOptions {
  pairingService: DevicePairingService;
}

const pairingSchema = z.object({
  code: z.string().trim().min(6).max(32),
  displayName: z.string().trim().min(1).max(64),
  publicKeyPem: z.string().trim().min(1).max(4_096),
});

const refreshSchema = z.object({
  refreshToken: z.string().trim().min(1).max(512),
});

function invalidArgument(message: string): ApplicationError {
  return new ApplicationError({
    code: "INVALID_ARGUMENT",
    message,
    statusCode: 400,
    retryable: false,
  });
}

/**
 * 注册设备配对与令牌签发路由。
 *
 * 这三条路由是设备凭据的合法签发来源，因此不要求既有设备认证：配对码短时、
 * 一次性且仅由服务端持有；刷新凭据轮换自带重放检测与同族撤销。其余业务
 * 路由仍全部要求设备访问令牌。
 */
export function registerDeviceRoutes(
  app: FastifyInstance,
  options: DeviceRouteOptions,
): void {
  app.post("/v1/devices/pairing-codes", async (_request, reply) => {
    const challenge = options.pairingService.createPairingCode();
    reply.code(201);
    return challenge;
  });

  app.post("/v1/devices/pair", async (request, reply) => {
    const body = pairingSchema.safeParse(request.body);
    if (!body.success) {
      throw invalidArgument("设备配对参数无效");
    }
    const result = await options.pairingService.pairDevice({
      code: body.data.code,
      displayName: body.data.displayName,
      publicKeyPem: body.data.publicKeyPem,
    });
    reply.code(201);
    return result;
  });

  app.post("/v1/devices/tokens", async (request) => {
    const body = refreshSchema.safeParse(request.body);
    if (!body.success) {
      throw invalidArgument("设备令牌刷新参数无效");
    }
    return options.pairingService.refreshTokens({
      refreshToken: body.data.refreshToken,
    });
  });
}
