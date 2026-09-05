import type { FastifyInstance, FastifyRequest } from "fastify";
import { z } from "zod";

import type { GmailService, GmailSyncService } from "../application/index.js";
import { ApplicationError } from "../application/index.js";
import type { PairedDeviceAuthRecord } from "../infrastructure/security/device-auth.js";

/** Gmail 连接、映射、授权事务与同步路由的装配依赖。 */
export interface GmailRouteOptions {
  gmailService: GmailService;
  syncService: GmailSyncService;
  authenticateDevice: (token: string) => Promise<PairedDeviceAuthRecord>;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu;

const completeSchema = z.object({
  state: z.string().trim().regex(/^[A-Za-z0-9_-]{43}$/),
  code: z.string().trim().min(1).max(1_024),
});

const updateMappingSchema = z.object({
  accountId: z.string().regex(UUID_PATTERN),
});

const disconnectSchema = z.object({
  revoke: z.coerce.boolean().default(false),
});

const syncSchema = z.object({
  /** 用户确认的首次扫描或重扫起始日（YYYY-MM-DD）；增量同步不需要。 */
  confirmedRescanAfterDate: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
  /** 首次映射确认时用户明确选择的账户；仅对单一未映射尾号有效。 */
  explicitAccountId: z.string().regex(UUID_PATTERN).optional(),
});

function unauthorized(): ApplicationError {
  return new ApplicationError({
    code: "DEVICE_CREDENTIAL_INVALID",
    message: "设备访问令牌无效",
    statusCode: 401,
    retryable: false,
  });
}

function invalidArgument(message: string): ApplicationError {
  return new ApplicationError({
    code: "INVALID_ARGUMENT",
    message,
    statusCode: 400,
    retryable: false,
  });
}

/**
 * 注册 Gmail 连接、授权事务、映射与断开路由。
 *
 * 全部路由要求设备访问令牌认证；授权事务与当前设备绑定，一次性 state 在
 * complete 时消费。同步触发路由随同步编排器另行挂接。
 */
export function registerGmailRoutes(
  app: FastifyInstance,
  options: GmailRouteOptions,
): void {
  const authenticate = async (request: FastifyRequest): Promise<PairedDeviceAuthRecord> => {
    const header = request.headers.authorization;
    const token = typeof header === "string" && header.startsWith("Bearer ")
      ? header.slice("Bearer ".length)
      : null;
    if (token === null) {
      throw unauthorized();
    }
    return options.authenticateDevice(token);
  };

  app.post("/v1/google/authorization-transactions", { preHandler: authenticate }, async (request, reply) => {
    const device = await authenticate(request);
    const transaction = options.gmailService.createAuthorizationTransaction(device.id);
    if (transaction === null) {
      throw new ApplicationError({
        code: "GMAIL_CONNECTION_ERROR",
        message: "Google OAuth 客户端尚未配置",
        statusCode: 503,
        retryable: false,
      });
    }
    reply.code(201);
    return transaction;
  });

  app.post("/v1/google/authorization-transactions/:state/complete", { preHandler: authenticate }, async (request) => {
    const device = await authenticate(request);
    const state = (request.params as Record<string, unknown>).state;
    if (typeof state !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(state)) {
      throw invalidArgument("Google 授权事务标识无效");
    }
    const body = completeSchema.safeParse(request.body);
    if (!body.success) {
      throw invalidArgument("Google 授权完成参数无效");
    }
    return options.gmailService.completeAuthorizationTransaction({
      deviceId: device.id,
      state,
      authorizationCode: body.data.code,
    });
  });

  app.get("/v1/google/connections", { preHandler: authenticate }, async () => {
    const connections = await options.gmailService.listConnections();
    return { connections };
  });

  app.get("/v1/google/connections/:id/mappings", { preHandler: authenticate }, async (request) => {
    const connectionId = readConnectionId(request);
    const mappings = await options.gmailService.listMappings(connectionId);
    return { mappings };
  });

  app.put("/v1/google/connections/:id/mappings/:mappingId", { preHandler: authenticate }, async (request) => {
    const connectionId = readConnectionId(request);
    const mappingId = (request.params as Record<string, unknown>).mappingId;
    if (typeof mappingId !== "string" || !UUID_PATTERN.test(mappingId)) {
      throw invalidArgument("Gmail 映射 ID 无效");
    }
    const body = updateMappingSchema.safeParse(request.body);
    if (!body.success) {
      throw invalidArgument("Gmail 映射更新参数无效");
    }
    return {
      mapping: await options.gmailService.updateMapping({
        connectionId,
        mappingId,
        targetAccountId: body.data.accountId,
      }),
    };
  });

  app.post("/v1/google/connections/:id/sync", { preHandler: authenticate }, async (request) => {
    const connectionId = readConnectionId(request);
    const body = syncSchema.safeParse(request.body ?? {});
    if (!body.success) {
      throw invalidArgument("Gmail 同步参数无效");
    }
    return options.syncService.sync({
      connectionId,
      confirmedRescanAfterDate: body.data.confirmedRescanAfterDate ?? null,
      explicitAccountId: body.data.explicitAccountId ?? null,
    });
  });

  app.delete("/v1/google/connections/:id", { preHandler: authenticate }, async (request) => {
    const connectionId = readConnectionId(request);
    const query = (request.query ?? {}) as Readonly<Record<string, unknown>>;
    const revoke = disconnectSchema.safeParse({ revoke: query.revoke });
    return {
      connection: await options.gmailService.disconnect({
        connectionId,
        revoke: revoke.success ? revoke.data.revoke : false,
      }),
    };
  });
}

function readConnectionId(request: FastifyRequest): string {
  const id = (request.params as Record<string, unknown>).id;
  if (typeof id !== "string" || !UUID_PATTERN.test(id)) {
    throw invalidArgument("Gmail 连接 ID 无效");
  }
  return id;
}
