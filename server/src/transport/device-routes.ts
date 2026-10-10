import type { FastifyInstance } from "fastify";
import { z } from "zod";

import type { DevicePairingService } from "../application/device-pairing-service.js";
import { ApplicationError } from "../application/app-error.js";

export interface DeviceRouteOptions {
  pairingService: DevicePairingService;
}

const refreshSchema = z.object({
  refreshToken: z.string().trim().min(1).max(512),
}).strict();

/** 仅保留已登录凭据轮换；匿名配对码创建和配对入口已经移除。 */
export function registerDeviceRoutes(app: FastifyInstance, options: DeviceRouteOptions): void {
  app.post("/v1/devices/tokens", async (request, reply) => {
    reply.header("cache-control", "no-store");
    const body = refreshSchema.safeParse(request.body);
    if (!body.success) {
      throw new ApplicationError({
        code: "INVALID_ARGUMENT", message: "设备令牌刷新参数无效", statusCode: 400, retryable: false,
      });
    }
    return options.pairingService.refreshTokens({ refreshToken: body.data.refreshToken });
  });
}
