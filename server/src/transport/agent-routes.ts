import type { FastifyInstance, FastifyRequest } from "fastify";
import { z } from "zod";

import type {
  AgentAvailabilitySnapshot,
  AgentTurnOrchestrator,
  WriteCoordinator,
} from "../application/index.js";
import {
  closeAgentSessionForRequest,
  createAgentSessionForRequest,
  createPublicAgentSession,
  createPublicAgentToolCallList,
} from "../application/index.js";
import { ApplicationError } from "../application/index.js";
import type { PairedDeviceAuthRecord } from "../infrastructure/security/device-auth.js";
import { loadAgentSession } from "../infrastructure/db/agent-session-repository.js";
import { loadAgentToolCalls } from "../infrastructure/db/agent-tool-call-repository.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";

declare module "fastify" {
  interface FastifyRequest {
    agentDevice?: PairedDeviceAuthRecord;
  }
}

/** Agent 会话路由所需的装配依赖。 */
export interface AgentRouteOptions {
  orchestrator: AgentTurnOrchestrator;
  writeCoordinator: WriteCoordinator;
  database: LyraDatabase;
  availability: () => Promise<AgentAvailabilitySnapshot>;
  authenticateDevice: (token: string) => Promise<PairedDeviceAuthRecord>;
  now?: () => Date;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu;

const createSessionSchema = z.object({
  provider: z.literal("genkit").default("genkit"),
  accountScope: z.discriminatedUnion("kind", [
    z.object({ kind: z.literal("all") }),
    z.object({ kind: z.literal("account"), account: z.string().regex(UUID_PATTERN) }),
  ]),
});

const sendMessageSchema = z.object({
  content: z.string().trim().min(1).max(8_192),
});

const sessionIdSchema = z.object({
  sessionId: z.string().regex(UUID_PATTERN),
});

/** 认证失败统一转为稳定 401 应用错误，由全局错误序列化器输出。 */
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
 * 注册 Agent 可用性与会话路由。
 *
 * 所有路由都要求设备访问令牌认证；会话读快照直接来自规范会话仓储，与回合
 * 编排状态无关，App 重连在回合进行中也能取到 running 快照。
 */
export function registerAgentRoutes(
  app: FastifyInstance,
  options: AgentRouteOptions,
): void {
  const now = options.now ?? (() => new Date());
  const authenticate = async (request: FastifyRequest): Promise<void> => {
    const header = request.headers.authorization;
    const token = typeof header === "string" && header.startsWith("Bearer ")
      ? header.slice("Bearer ".length)
      : null;
    if (token === null) {
      throw unauthorized();
    }
    request.agentDevice = await options.authenticateDevice(token);
  };

  app.get("/v1/agents", { preHandler: authenticate }, async (request) => {
    void request;
    return options.availability();
  });

  app.post("/v1/agent/sessions", { preHandler: authenticate }, async (request, reply) => {
    const parsed = createSessionSchema.safeParse(request.body);
    if (!parsed.success) {
      throw invalidArgument("Agent 会话创建参数无效");
    }
    const device = request.agentDevice;
    if (device === undefined) {
      throw unauthorized();
    }
    const session = await createAgentSessionForRequest({
      orchestrator: options.orchestrator,
      writeCoordinator: options.writeCoordinator,
      database: options.database,
      availability: await options.availability(),
      deviceId: device.id,
      request: parsed.data,
      now: now(),
    });
    reply.code(201);
    return { session: createPublicAgentSession(session) };
  });

  app.get("/v1/agent/sessions/:sessionId", { preHandler: authenticate }, async (request, reply) => {
    const parsed = sessionIdSchema.safeParse(request.params);
    if (!parsed.success) {
      throw invalidArgument("Agent 会话 ID 无效");
    }
    const session = await loadAgentSession(options.database, parsed.data.sessionId);
    if (session === null) {
      return reply.code(404).send({
        request_id: request.id,
        code: "AGENT_SESSION_STATE_CONFLICT",
        message: "Agent 会话不存在",
        retryable: false,
      });
    }
    return { session: createPublicAgentSession(session) };
  });

  app.get("/v1/agent/sessions/:sessionId/tool-calls", { preHandler: authenticate }, async (request, reply) => {
    const parsed = sessionIdSchema.safeParse(request.params);
    if (!parsed.success) {
      throw invalidArgument("Agent 会话 ID 无效");
    }
    const calls = await loadAgentToolCalls(options.database, parsed.data.sessionId);
    if (calls === null) {
      return reply.code(404).send({
        request_id: request.id,
        code: "AGENT_SESSION_STATE_CONFLICT",
        message: "Agent 会话不存在",
        retryable: false,
      });
    }
    return { toolCalls: createPublicAgentToolCallList(calls) };
  });

  app.post("/v1/agent/sessions/:sessionId/messages", { preHandler: authenticate }, async (request) => {
    const params = sessionIdSchema.safeParse(request.params);
    const body = sendMessageSchema.safeParse(request.body);
    if (!params.success || !body.success) {
      throw invalidArgument("Agent 回合参数无效");
    }
    const run = await options.orchestrator.startTurn(
      params.data.sessionId, body.data.content, () => authenticate(request),
    );
    return {
      session: createPublicAgentSession(run.snapshot),
      events: run.events,
    };
  });

  app.post("/v1/agent/sessions/:sessionId/cancel", { preHandler: authenticate }, async (request) => {
    const params = sessionIdSchema.safeParse(request.params);
    if (!params.success) {
      throw invalidArgument("Agent 会话 ID 无效");
    }
    const snapshot = await options.orchestrator.cancelTurn(params.data.sessionId);
    return { session: createPublicAgentSession(snapshot) };
  });

  app.delete("/v1/agent/sessions/:sessionId", { preHandler: authenticate }, async (request) => {
    const params = sessionIdSchema.safeParse(request.params);
    if (!params.success) {
      throw invalidArgument("Agent 会话 ID 无效");
    }
    const snapshot = await closeAgentSessionForRequest({
      orchestrator: options.orchestrator,
      writeCoordinator: options.writeCoordinator,
      database: options.database,
      sessionId: params.data.sessionId,
      now: now(),
    });
    return { session: createPublicAgentSession(snapshot) };
  });
}
