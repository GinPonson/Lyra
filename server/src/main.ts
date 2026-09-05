import { mkdirSync, readFileSync } from "node:fs";
import process from "node:process";

import { z } from "zod";

import {
  WriteCoordinator,
  assembleAgentMcpRuntime,
  createAgentAvailabilityProvider,
  createDeviceAuthGate,
  createServiceHealthSnapshot,
} from "./application/index.js";
import { AgentTurnOrchestrator } from "./application/index.js";
import { AgentMcpCredentialStore, createAgentTurnExecutors } from "./infrastructure/agents/index.js";
import { openLyraDatabase } from "./infrastructure/db/database.js";
import { CURRENT_SCHEMA_VERSION } from "./infrastructure/db/schema-version.js";
import {
  buildHttpServer,
  createServerProtocolMetadata,
} from "./transport/index.js";
import { registerAgentRoutes } from "./transport/agent-routes.js";
import { registerAgentEventsRoute } from "./transport/agent-events.js";
import { registerLedgerRoutes } from "./transport/ledger-routes.js";
import { registerImportRoutes } from "./transport/import-routes.js";
import { registerGmailRoutes } from "./transport/gmail-routes.js";
import { registerDeviceRoutes } from "./transport/device-routes.js";
import { ImportService, GmailService, GmailSyncService, DevicePairingService } from "./application/index.js";
import { SecretCipher } from "./infrastructure/security/index.js";

const environmentSchema = z.object({
  LYRA_HOST: z.string().trim().min(1).default("127.0.0.1"),
  LYRA_PORT: z.coerce.number().int().min(1).max(65_535).default(3_141),
  LYRA_DATABASE_PATH: z.string().trim().min(1),
  LYRA_AGENT_WORKSPACE_ROOT: z.string().trim().min(1),
  LYRA_AGENT_CLAUDE_PATH: z.string().trim().min(1),
  LYRA_AGENT_PI_PATH: z.string().trim().min(1),
  LYRA_AGENT_OPENCODE_PATH: z.string().trim().min(1),
  LYRA_AGENT_PI_EXTENSION_PATH: z.string().trim().min(1),
  LYRA_AGENT_TURN_TIMEOUT_MS: z.coerce.number().int().min(1_000).default(900_000),
  LYRA_AGENT_PROBE_TIMEOUT_MS: z.coerce.number().int().min(1_000).default(10_000),
  LYRA_IMPORT_STAGING_DIR: z.string().trim().min(1),
  /** 32 字节 hex 主密钥；Gmail refresh token 等敏感数据经 AES-256-GCM 加密。 */
  LYRA_SECRET_MASTER_KEY: z.string().regex(/^[0-9a-fA-F]{64}$/),
  LYRA_GOOGLE_CLIENT_ID: z.string().trim().min(1).default(""),
  LYRA_GOOGLE_CLIENT_SECRET: z.string().trim().min(1).default(""),
  LYRA_GOOGLE_REDIRECT_URI: z.string().trim().min(1).default(""),
  /** JSON 对象：{"claude":["2.1.233"],"pi":["0.84.2"],"opencode":["1.18.21"]}。 */
  LYRA_AGENT_ALLOWED_VERSIONS: z.string().trim().min(1),
});

const allowedVersionsSchema = z.record(
  z.enum(["claude", "pi", "opencode"]),
  z.array(z.string().trim().min(1)).min(1),
);

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
  // MCP 短期凭据只在 Agent 回合运行期间存在于进程内；服务重启即失效。
  const mcpCredentials = new AgentMcpCredentialStore();
  const mcpRuntime = assembleAgentMcpRuntime({ database, mcpCredentials });
  // 会话工作目录在受信根路径下随机创建；根目录本身按配置在启动时准备。
  mkdirSync(environment.LYRA_AGENT_WORKSPACE_ROOT, { recursive: true });
  mkdirSync(environment.LYRA_IMPORT_STAGING_DIR, { recursive: true });
  const importService = new ImportService({
    database,
    writeCoordinator,
    stagingDirectory: environment.LYRA_IMPORT_STAGING_DIR,
  });
  const cipher = new SecretCipher(
    Buffer.from(environment.LYRA_SECRET_MASTER_KEY, "hex"),
  );
  const googleOAuth = environment.LYRA_GOOGLE_CLIENT_ID.length > 0
      && environment.LYRA_GOOGLE_REDIRECT_URI.length > 0
    ? {
        clientId: environment.LYRA_GOOGLE_CLIENT_ID,
        ...(environment.LYRA_GOOGLE_CLIENT_SECRET.length > 0
          ? { clientSecret: environment.LYRA_GOOGLE_CLIENT_SECRET }
          : {}),
        redirectUri: environment.LYRA_GOOGLE_REDIRECT_URI,
      }
    : null;
  const gmailService = new GmailService({
    database,
    cipher,
    googleOAuth,
  });
  const gmailSyncService = new GmailSyncService({
    database,
    writeCoordinator,
    cipher,
    googleOAuth,
  });
  const devicePairingService = new DevicePairingService({
    database,
    writeCoordinator,
  });
  const executables = {
    claude: environment.LYRA_AGENT_CLAUDE_PATH,
    pi: environment.LYRA_AGENT_PI_PATH,
    opencode: environment.LYRA_AGENT_OPENCODE_PATH,
  } as const;
  const orchestrator = new AgentTurnOrchestrator({
    database,
    writeCoordinator,
    mcpCredentials,
    registry: mcpRuntime.registry,
    executors: createAgentTurnExecutors({
      workspaceRoot: environment.LYRA_AGENT_WORKSPACE_ROOT,
      clientVersion: packageJson.version,
      executables,
      piExtensionPath: environment.LYRA_AGENT_PI_EXTENSION_PATH,
      turnTimeoutMilliseconds: environment.LYRA_AGENT_TURN_TIMEOUT_MS,
    }),
    mcpEndpoint: `http://127.0.0.1:${environment.LYRA_PORT}/mcp`,
    mcpCredentialTtlMilliseconds: 120_000,
  });
  const availability = createAgentAvailabilityProvider({
    executables,
    allowedVersions: allowedVersionsSchema.parse(
      JSON.parse(environment.LYRA_AGENT_ALLOWED_VERSIONS),
    ),
    probeTimeoutMilliseconds: environment.LYRA_AGENT_PROBE_TIMEOUT_MS,
  });
  const authenticateDevice = createDeviceAuthGate(database);
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
    mcp: {
      registry: mcpRuntime.registry,
      loadAuthorization: mcpRuntime.loadAuthorization,
      persistCredentialRecord: mcpRuntime.persistCredentialRecord,
    },
  });
  registerAgentRoutes(app, {
    orchestrator,
    writeCoordinator,
    database,
    availability,
    authenticateDevice,
  });
  await registerAgentEventsRoute(app, { orchestrator, authenticateDevice });
  registerLedgerRoutes(app, {
    registry: mcpRuntime.registry,
    database,
    authenticateDevice,
  });
  registerImportRoutes(app, {
    importService,
    authenticateDevice,
  });
  registerGmailRoutes(app, {
    gmailService,
    syncService: gmailSyncService,
    authenticateDevice,
  });
  registerDeviceRoutes(app, { pairingService: devicePairingService });
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
