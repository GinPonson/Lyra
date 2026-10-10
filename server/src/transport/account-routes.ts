import type { FastifyInstance, FastifyRequest } from "fastify";

import type { AccountService } from "../application/account-service.js";
import { ApplicationError } from "../application/app-error.js";
import type { PairedDeviceAuthRecord } from "../infrastructure/security/device-auth.js";

export interface AccountRouteOptions {
  accountService: AccountService;
  authenticateDevice: (token: string) => Promise<PairedDeviceAuthRecord>;
}

/** 账户写入口与只读账本 / Agent 工具独立，只接受当前已授权设备的请求。 */
export function registerAccountRoutes(app: FastifyInstance, options: AccountRouteOptions): void {
  app.post("/v1/ledger/accounts", {
    preHandler: async (request) => { await options.authenticateDevice(accessToken(request)); },
  }, async (request, reply) => {
    const key = request.headers["idempotency-key"];
    if (key !== undefined && (typeof key !== "string" || !/^[A-Za-z0-9._:-]{16,200}$/u.test(key))) {
      throw new ApplicationError({
        code: "INVALID_ARGUMENT", message: "幂等键必须是 16 至 200 位安全字符", statusCode: 400, retryable: false,
      });
    }
    const token = accessToken(request);
    const result = await options.accountService.createAccount({
      request: request.body,
      idempotencyKey: key ?? null,
      requestId: request.id,
      authorize: () => options.authenticateDevice(token),
    });
    return reply.header("cache-control", "no-store").code(201).send(result);
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
