import { readFileSync } from "node:fs";
import process from "node:process";

import { z } from "zod";

import {
  WriteCoordinator,
  createServiceHealthSnapshot,
} from "./application/index.js";
import { openLyraDatabase } from "./infrastructure/db/database.js";
import { CURRENT_SCHEMA_VERSION } from "./infrastructure/db/schema-version.js";
import {
  buildHttpServer,
  createServerProtocolMetadata,
} from "./transport/index.js";

const environmentSchema = z.object({
  LYRA_HOST: z.string().trim().min(1).default("127.0.0.1"),
  LYRA_PORT: z.coerce.number().int().min(1).max(65_535).default(3_141),
  LYRA_DATABASE_PATH: z.string().trim().min(1),
});

const packageSchema = z.object({
  version: z.string().trim().min(1),
});

/** 启动当前唯一 TypeScript 服务进程。 */
async function start(): Promise<void> {
  const environment = environmentSchema.parse(process.env);
  const packageJson = packageSchema.parse(JSON.parse(
    readFileSync(new URL("../../package.json", import.meta.url), "utf8"),
  ));
  const database = await openLyraDatabase(environment.LYRA_DATABASE_PATH);
  const writeCoordinator = new WriteCoordinator();
  const app = buildHttpServer({
    logger: true,
    protocolMetadata: createServerProtocolMetadata(
      packageJson.version,
      CURRENT_SCHEMA_VERSION,
    ),
    getHealthSnapshot: async () => createServiceHealthSnapshot({
      database: await database.readHealth(),
      writeQueue: writeCoordinator.status(),
      gmailConnections: [],
      agentAvailability: null,
      checkedAt: new Date(),
    }),
  });
  app.addHook("onClose", async () => {
    await writeCoordinator.close();
    database.close();
  });

  let closing = false;
  const close = async (signal: "SIGINT" | "SIGTERM"): Promise<void> => {
    if (closing) {
      return;
    }
    closing = true;
    app.log.info({ signal }, "server shutdown started");
    try {
      await app.close();
    } catch (error: unknown) {
      app.log.error(
        { errorName: error instanceof Error ? error.name : "UnknownError" },
        "server shutdown failed",
      );
      process.exitCode = 1;
    }
  };
  process.once("SIGINT", () => void close("SIGINT"));
  process.once("SIGTERM", () => void close("SIGTERM"));

  await app.listen({ host: environment.LYRA_HOST, port: environment.LYRA_PORT });
}

try {
  await start();
} catch (error: unknown) {
  // 启动失败只输出错误类型；配置值、数据库路径和底层 SQL 不进入终端日志。
  process.stderr.write(`${JSON.stringify({
    level: "error",
    errorName: error instanceof Error ? error.name : "UnknownError",
    message: "server startup failed",
  })}\n`);
  process.exitCode = 1;
}
