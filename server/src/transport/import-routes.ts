import type { FastifyInstance, FastifyRequest } from "fastify";
import type { Readable } from "node:stream";
import { z } from "zod";

import type { ImportService } from "../application/index.js";
import { ApplicationError } from "../application/index.js";
import type { PairedDeviceAuthRecord } from "../infrastructure/security/device-auth.js";

/** 导入路由所需的装配依赖。 */
export interface ImportRouteOptions {
  importService: ImportService;
  authenticateDevice: (token: string) => Promise<PairedDeviceAuthRecord>;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu;

const importIdSchema = z.object({
  importId: z.string().regex(UUID_PATTERN),
});

const commitSchema = z.object({
  confirmToken: z.string().trim().min(1).max(512),
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

function authenticateDeviceToken(request: FastifyRequest): string {
  const header = request.headers.authorization;
  const token = typeof header === "string" && header.startsWith("Bearer ")
    ? header.slice("Bearer ".length)
    : null;
  if (token === null) {
    throw unauthorized();
  }
  return token;
}

/** 文件名只用于展示与预览投影，不参与任何服务端路径拼接。 */
function readUploadFileName(request: FastifyRequest): string {
  const header = request.headers["x-lyra-file-name"];
  const value = Array.isArray(header) ? header[0] : header;
  if (typeof value !== "string" || value.trim().length === 0 || value.length > 256) {
    throw invalidArgument("上传请求必须携带有效的 x-lyra-file-name 头");
  }
  return value;
}

/**
 * 注册导入上传、预览、状态、提交与取消路由。
 *
 * 上传使用 application/octet-stream 原始字节流，由服务端流式落盘并计算哈希；
 * 提交必须携带预览签发的一次性确认令牌。全部路由要求设备访问令牌认证。
 */
export function registerImportRoutes(
  app: FastifyInstance,
  options: ImportRouteOptions,
): void {
  if (!octetStreamParsers.has(app)) {
    // Fastify 5：无 parseAs 的解析器直接接收原始请求流（IncomingMessage）。
    // bodyLimit 略高于上传监控上限，让稳定的领域错误优先于框架 413。
    app.addContentTypeParser(
      "application/octet-stream",
      { bodyLimit: MAX_UPLOAD_BODY_BYTES },
      (request, payload, done) => {
        void request;
        done(null, payload);
      },
    );
    octetStreamParsers.add(app);
  }

  const authenticate = async (request: FastifyRequest): Promise<void> => {
    await options.authenticateDevice(authenticateDeviceToken(request));
  };

  app.post("/v1/imports/preview", { preHandler: authenticate }, async (request, reply) => {
    const body = request.body;
    if (body === null || typeof body !== "object" || !("pipe" in body)) {
      throw invalidArgument("导入上传必须使用 application/octet-stream 请求体");
    }
    const query = (request.query ?? {}) as Readonly<Record<string, unknown>>;
    const explicitAccountId = typeof query.accountId === "string"
      && UUID_PATTERN.test(query.accountId)
      ? query.accountId
      : null;
    const result = await options.importService.createPreviewFromUpload({
      stream: body as unknown as Readable,
      fileName: readUploadFileName(request),
      explicitAccountId,
    });
    reply.code(201);
    return result;
  });

  app.get("/v1/imports/:importId", { preHandler: authenticate }, async (request, reply) => {
    const params = importIdSchema.safeParse(request.params);
    if (!params.success) {
      throw invalidArgument("导入作业 ID 无效");
    }
    const job = await options.importService.getImportJob(params.data.importId);
    if (job === null) {
      return reply.code(404).send({
        request_id: request.id,
        code: "IMPORT_STATE_CONFLICT",
        message: "导入作业不存在",
        retryable: false,
      });
    }
    return { job };
  });

  app.post("/v1/imports/:importId/commit", { preHandler: authenticate }, async (request) => {
    const params = importIdSchema.safeParse(request.params);
    const body = commitSchema.safeParse(request.body);
    if (!params.success || !body.success) {
      throw invalidArgument("导入提交参数无效");
    }
    return options.importService.commitImport({
      importId: params.data.importId,
      confirmToken: body.data.confirmToken,
    });
  });

  app.post("/v1/imports/:importId/cancel", { preHandler: authenticate }, async (request) => {
    const params = importIdSchema.safeParse(request.params);
    if (!params.success) {
      throw invalidArgument("导入作业 ID 无效");
    }
    await options.importService.cancelImport(params.data.importId);
    return { cancelled: true };
  });
}

const octetStreamParsers = new WeakSet<FastifyInstance>();

/** 上传体框架限制；真实的大小与格式检查由 ImportUploadMonitor 执行。 */
const MAX_UPLOAD_BODY_BYTES = 21 * 1024 * 1024;
