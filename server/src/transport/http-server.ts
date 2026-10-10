import { randomUUID } from "node:crypto";

import Fastify, { type FastifyInstance } from "fastify";

import type { ServiceHealthSnapshot } from "../application/index.js";
import { serializeApiError } from "./api-error.js";
import {
  assertClientProtocolVersion,
  type ServerProtocolMetadata,
} from "./protocol-version.js";

/** 构建 HTTP 服务所需的最小运行时依赖。 */
export interface HttpServerOptions {
  protocolMetadata: ServerProtocolMetadata;
  getHealthSnapshot: () => ServiceHealthSnapshot | Promise<ServiceHealthSnapshot>;
  logger?: boolean;
}

const PUBLIC_V1_ROUTES = new Set<string>([
  "/v1/system/info",
  "/v1/health",
  "/v1/auth/google/callback",
]);

/**
 * 构建 Lyra 单一 Fastify 服务。
 *
 * 系统信息与健康检查用于 App 首次发现服务端，因此不要求协议请求头；其他
 * `/v1` 路由统一校验 `X-Lyra-Protocol-Version`。业务路由后续直接注册到返回的
 * Fastify 实例即可复用请求 ID、协议门禁和安全错误响应。
 *
 * @param options - 已验证的协议元数据、健康快照提供器和日志开关。
 * @returns 尚未监听端口的 Fastify 实例，便于生产启动与注入测试共用。
 */
export function buildHttpServer(options: HttpServerOptions): FastifyInstance {
  const app = Fastify({
    logger: options.logger ? {
      serializers: {
        // OAuth 回调的 code/state 与其他查询参数不进入访问日志。
        req(request) {
          return {
            method: request.method,
            url: request.url?.split("?")[0] ?? "/",
            hostname: request.hostname,
            remoteAddress: request.ip,
          };
        },
      },
    } : false,
    genReqId: () => randomUUID(),
  });

  app.addHook("onRequest", async (request, reply) => {
    reply.header("x-request-id", request.id);
    if (request.url.startsWith("/v1/auth/")) {
      reply.header("cache-control", "no-store");
      reply.header("referrer-policy", "no-referrer");
      reply.header("x-content-type-options", "nosniff");
    }

    if (
      request.url.startsWith("/v1/")
      && !PUBLIC_V1_ROUTES.has(request.routeOptions.url ?? request.url)
    ) {
      const rawVersion = request.headers["x-lyra-protocol-version"];
      assertClientProtocolVersion(Array.isArray(rawVersion) ? undefined : rawVersion);
    }
  });

  app.setErrorHandler((error, request, reply) => {
    const serialized = serializeApiError(error, request.id);
    request.log.error(
      {
        requestId: request.id,
        errorName: error instanceof Error ? error.name : "UnknownError",
      },
      "request failed",
    );
    void reply.code(serialized.statusCode).send(serialized.body);
  });

  app.get("/v1/system/info", async () => options.protocolMetadata);

  app.get("/v1/health", async (_request, reply) => {
    const snapshot = await options.getHealthSnapshot();
    const statusCode = snapshot.status === "unavailable" ? 503 : 200;
    return reply.code(statusCode).send(snapshot);
  });

  return app;
}
