import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";

import { ApplicationError } from "../application/app-error.js";
import type { MarketSettingsService } from "../application/market-settings-service.js";
import type { PairedDeviceAuthRecord } from "../infrastructure/security/device-auth.js";

export interface MarketSettingsRouteOptions {
  marketSettingsService: MarketSettingsService;
  authenticateDevice: (token: string) => Promise<PairedDeviceAuthRecord>;
}

/** All paired devices belong to the private server owner; selections are shared. */
export function registerMarketSettingsRoutes(app: FastifyInstance, options: MarketSettingsRouteOptions): void {
  const authenticate = async (request: FastifyRequest) => {
    await options.authenticateDevice(accessToken(request));
    if (Object.keys(request.query as Record<string, unknown>).length > 0) {
      throw new ApplicationError({
        code: "INVALID_ARGUMENT", message: "行情设置不接受查询参数", statusCode: 400, retryable: false,
      });
    }
  };
  const routeOptions = {
    onRequest: async (_request: FastifyRequest, reply: FastifyReply) => {
      reply.header("cache-control", "no-store");
    },
    preHandler: authenticate,
  };
  app.get("/v1/market/settings", routeOptions, async () => options.marketSettingsService.getSettings());
  app.put("/v1/market/settings", { ...routeOptions, bodyLimit: 2048 }, async (request) => {
    const token = accessToken(request);
    return options.marketSettingsService.save({
      request: request.body,
      requestId: request.id,
      authorize: () => options.authenticateDevice(token),
    });
  });
}

function accessToken(request: FastifyRequest): string {
  const header = request.headers.authorization;
  if (typeof header !== "string" || !header.startsWith("Bearer ") || header.length <= 7) {
    throw new ApplicationError({
      code: "DEVICE_CREDENTIAL_INVALID", message: "设备访问令牌无效", statusCode: 401, retryable: false,
    });
  }
  return header.slice("Bearer ".length);
}
