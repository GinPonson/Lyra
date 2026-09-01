import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import {
  CallToolRequestSchema,
  ErrorCode,
  ListToolsRequestSchema,
  McpError,
} from "@modelcontextprotocol/sdk/types.js";
import { AjvJsonSchemaValidator } from "@modelcontextprotocol/sdk/validation/ajv";
import type {
  JsonSchemaType,
  JsonSchemaValidator,
} from "@modelcontextprotocol/sdk/validation";
import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";

import { invokeAuthenticatedAgentTool } from "../application/index.js";
import {
  AgentMcpCredentialError,
  AgentToolPolicyError,
  type AgentSessionSnapshot,
  type FinancialToolDefinition,
  type FinancialToolRegistry,
} from "../domain/agent/index.js";
import {
  authenticateAgentMcpCredential,
  type AgentMcpCredentialRecord,
} from "../infrastructure/agents/index.js";
import { hashOpaqueCredential } from "../infrastructure/security/index.js";

/** 根据令牌哈希取得一次 MCP 请求所需的服务端权威快照。 */
export interface AgentMcpAuthorizationSnapshot {
  session: AgentSessionSnapshot;
  credentialRecord: AgentMcpCredentialRecord;
}

/** 官方 MCP HTTP 路由与现有应用层之间的最小依赖。 */
export interface AgentMcpHttpOptions {
  registry: FinancialToolRegistry;
  loadAuthorization: (
    tokenHash: string,
  ) => Promise<AgentMcpAuthorizationSnapshot | null>;
  persistCredentialRecord: (record: AgentMcpCredentialRecord) => Promise<void>;
  now?: () => Date;
}

interface AuthorizedRequest {
  authorization: AgentMcpAuthorizationSnapshot;
  token: string;
  now: Date;
}

type ToolValidator = JsonSchemaValidator<Readonly<Record<string, unknown>>>;

/**
 * 在 Lyra 单一 Fastify 进程注册官方 MCP Streamable HTTP 端点。
 *
 * 端点采用 SDK 官方无状态模式：不创建第二套会话 ID，每个 POST 都重新验证
 * 已绑定 Agent 会话的短期 Bearer。GET/SSE 与 DELETE 不属于当前只读请求响应
 * 模式，因此明确返回 405。
 *
 * @param app - 与移动端 API 共用的 Fastify 实例。
 * @param serverVersion - 公开给 MCP initialize 的 Lyra 服务版本。
 * @param options - 凭据仓储、规范会话仓储和单一金融工具注册表。
 */
export function registerAgentMcpHttpRoutes(
  app: FastifyInstance,
  serverVersion: string,
  options: AgentMcpHttpOptions,
): void {
  const normalizedVersion = serverVersion.trim();
  if (normalizedVersion.length === 0) {
    throw new RangeError("MCP 服务版本不能为空");
  }
  const definitions = options.registry.availableToolDefinitions();
  const validators = compileToolValidators(definitions);

  app.route({
    method: ["GET", "POST", "DELETE"],
    url: "/mcp",
    handler: async (request, reply) => {
      if (request.method !== "POST") {
        return sendMcpError(
          reply,
          405,
          ErrorCode.MethodNotFound,
          "MCP 无状态端点仅接受 POST",
        );
      }

      const authorized = await authorizeRequest(request, reply, options);
      if (authorized === null) {
        return;
      }
      await handleMcpPost(
        request,
        reply,
        normalizedVersion,
        options,
        definitions,
        validators,
        authorized,
      );
    },
  });
}

/** 在进入 MCP SDK 前完成 Bearer、会话 revision、范围和时效认证。 */
async function authorizeRequest(
  request: FastifyRequest,
  reply: FastifyReply,
  options: AgentMcpHttpOptions,
): Promise<AuthorizedRequest | null> {
  const token = readBearerToken(request.headers.authorization);
  if (token === null) {
    reply.header("www-authenticate", "Bearer realm=\"lyra-mcp\"");
    await sendMcpError(reply, 401, ErrorCode.ConnectionClosed, "MCP 会话授权无效");
    return null;
  }

  try {
    const authorization = await options.loadAuthorization(hashOpaqueCredential(token));
    if (authorization === null) {
      throw new AgentMcpCredentialError("invalid_token");
    }
    const now = (options.now ?? (() => new Date()))();
    const authenticated = authenticateAgentMcpCredential({
      session: authorization.session,
      record: authorization.credentialRecord,
      token,
      now,
    });
    await options.persistCredentialRecord(authenticated.record);
    return { authorization, token, now };
  } catch (error: unknown) {
    if (error instanceof AgentMcpCredentialError) {
      reply.header("www-authenticate", "Bearer realm=\"lyra-mcp\"");
      await sendMcpError(reply, 401, ErrorCode.ConnectionClosed, "MCP 会话授权无效");
      return null;
    }
    logMcpFailure(request, error, "MCP authorization persistence failed");
    await sendMcpError(reply, 500, ErrorCode.InternalError, "MCP 服务暂时不可用");
    return null;
  }
}

/** 把一个已认证 POST 交给官方无状态 transport，并在响应后释放实例。 */
async function handleMcpPost(
  request: FastifyRequest,
  reply: FastifyReply,
  serverVersion: string,
  options: AgentMcpHttpOptions,
  definitions: readonly FinancialToolDefinition[],
  validators: ReadonlyMap<string, ToolValidator>,
  authorized: AuthorizedRequest,
): Promise<void> {
  const server = createMcpServer(
    serverVersion,
    options.registry,
    definitions,
    validators,
    authorized,
    (error) => logMcpFailure(request, error, "MCP tool execution failed"),
  );
  const transport = new StreamableHTTPServerTransport({
    enableJsonResponse: true,
  });
  reply.hijack();
  try {
    // SDK 1.30 的 transport setter 类型在 exactOptionalPropertyTypes 下与自身
    // Transport 接口不兼容；运行时类明确实现该接口，因此只在此边界收窄类型。
    await server.connect(transport as unknown as Transport);
    await transport.handleRequest(request.raw, reply.raw, request.body);
  } catch (error: unknown) {
    logMcpFailure(request, error, "MCP transport failed");
    if (!reply.raw.headersSent) {
      writeRawMcpError(reply, 500, ErrorCode.InternalError, "MCP 服务暂时不可用");
    }
  } finally {
    try {
      await server.close();
    } catch (error: unknown) {
      logMcpFailure(request, error, "MCP transport close failed");
    }
  }
}

/** 使用低层官方 Server 保留既有 JSON Schema 单一来源，不复制为另一套 Zod。 */
function createMcpServer(
  serverVersion: string,
  registry: FinancialToolRegistry,
  definitions: readonly FinancialToolDefinition[],
  validators: ReadonlyMap<string, ToolValidator>,
  authorized: AuthorizedRequest,
  reportError: (error: unknown) => void,
): Server {
  const server = new Server(
    { name: "lyra", version: serverVersion },
    {
      capabilities: { tools: {} },
      instructions: "仅提供绑定当前 Lyra 会话数据口径的只读金融工具。",
    },
  );
  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: definitions.map((definition) => ({
      name: definition.name,
      description: definition.description,
      inputSchema: { ...definition.inputSchema },
      annotations: { ...definition.annotations },
    })),
  }));
  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const validator = validators.get(request.params.name);
    if (validator === undefined) {
      throw new McpError(ErrorCode.InvalidParams, "金融工具不存在或尚未启用");
    }
    const validation = validator(request.params.arguments ?? {});
    if (!validation.valid) {
      throw new McpError(ErrorCode.InvalidParams, "金融工具参数不符合公开定义");
    }

    try {
      const executed = await invokeAuthenticatedAgentTool({
        session: authorized.authorization.session,
        credentialRecord: authorized.authorization.credentialRecord,
        credentialToken: authorized.token,
        registry,
        toolName: request.params.name,
        arguments: validation.data,
        now: authorized.now,
      });
      const structuredContent = { ...executed.result };
      return {
        content: [{ type: "text" as const, text: JSON.stringify(structuredContent) }],
        structuredContent,
      };
    } catch (error: unknown) {
      if (error instanceof AgentMcpCredentialError) {
        throw new McpError(ErrorCode.ConnectionClosed, "MCP 会话授权已失效");
      }
      if (error instanceof AgentToolPolicyError) {
        throw new McpError(ErrorCode.InvalidParams, "金融工具不存在或尚未启用");
      }
      reportError(error);
      throw new McpError(ErrorCode.InternalError, "Lyra 金融工具执行失败");
    }
  });
  return server;
}

/** 用 SDK 自带 AJV 一次编译所有已配置工具的既有 JSON Schema。 */
function compileToolValidators(
  definitions: readonly FinancialToolDefinition[],
): ReadonlyMap<string, ToolValidator> {
  const provider = new AjvJsonSchemaValidator();
  return new Map(definitions.map((definition) => [
    definition.name,
    provider.getValidator<Readonly<Record<string, unknown>>>(
      definition.inputSchema as JsonSchemaType,
    ),
  ]));
}

/** 只接受签发器生成的单一 Base64URL Bearer，不接受多值或折行格式。 */
function readBearerToken(authorization: string | undefined): string | null {
  const match = /^Bearer ([A-Za-z0-9_-]{40,})$/u.exec(authorization ?? "");
  return match?.[1] ?? null;
}

/** Fastify 尚未被 SDK 接管时返回稳定 JSON-RPC 错误。 */
async function sendMcpError(
  reply: FastifyReply,
  statusCode: number,
  code: number,
  message: string,
): Promise<void> {
  await reply.code(statusCode).type("application/json").send({
    jsonrpc: "2.0",
    error: { code, message },
    id: null,
  });
}

/** SDK 接管 raw response 后的最后错误写回，不经过 Fastify 二次序列化。 */
function writeRawMcpError(
  reply: FastifyReply,
  statusCode: number,
  code: number,
  message: string,
): void {
  reply.raw.writeHead(statusCode, { "content-type": "application/json" });
  reply.raw.end(JSON.stringify({
    jsonrpc: "2.0",
    error: { code, message },
    id: null,
  }));
}

/** 日志仅包含关联 ID 与错误类型，不记录令牌、参数或底层自由文本。 */
function logMcpFailure(request: FastifyRequest, error: unknown, message: string): void {
  request.log.error({
    requestId: request.id,
    errorName: error instanceof Error ? error.name : "UnknownError",
  }, message);
}
