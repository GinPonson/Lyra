import { mkdirSync, readFileSync } from "node:fs";
import process from "node:process";

import { z } from "zod";

import {
  WriteCoordinator,
  AccountService,
  createFinancialToolRegistry,
  createDeviceAuthGate,
  createServiceHealthSnapshot,
} from "./application/index.js";
import { AgentTurnOrchestrator } from "./application/index.js";
import { createGenkitRuntimeFromEnvironment } from "./infrastructure/agents/genkit-runtime.js";
import { loadGoogleLoginConfig } from "./infrastructure/auth/google-login-client.js";
import { environmentSchema } from "./infrastructure/config/server-environment.js";
import { GoogleLoginService } from "./application/google-login-service.js";
import { registerGoogleLoginRoutes } from "./transport/google-login-routes.js";
import { openLyraDatabase } from "./infrastructure/db/database.js";
import { recoverInterruptedAgentSessions } from "./infrastructure/db/agent-session-repository.js";
import { CURRENT_SCHEMA_VERSION } from "./infrastructure/db/schema-version.js";
import {
  buildHttpServer,
  createServerProtocolMetadata,
} from "./transport/index.js";
import { registerAgentRoutes } from "./transport/agent-routes.js";
import { registerAgentEventsRoute } from "./transport/agent-events.js";
import { registerLedgerRoutes } from "./transport/ledger-routes.js";
import { registerAccountRoutes } from "./transport/account-routes.js";
import { registerImportRoutes } from "./transport/import-routes.js";
import { registerGmailRoutes } from "./transport/gmail-routes.js";
import { registerDeviceRoutes } from "./transport/device-routes.js";
import { ImportService, GmailService, GmailSyncService, DevicePairingService } from "./application/index.js";
import { SecretCipher } from "./infrastructure/security/index.js";

const packageSchema = z.object({
  version: z.string().trim().min(1),
});

/** 启动当前唯一 TypeScript 服务进程。 */
async function start(): Promise<void> {
  const environment = environmentSchema.parse(process.env);
  const packageJson = packageSchema.parse(JSON.parse(
    readFileSync(new URL("../../package.json", import.meta.url), "utf8"),
  ));
  // Validate new integration configuration before any irreversible schema upgrade.
  const loginConfig = loadGoogleLoginConfig(process.env);
  const aiRuntime = createGenkitRuntimeFromEnvironment(process.env);
  const database = await openLyraDatabase(environment.LYRA_DATABASE_PATH);
  const writeCoordinator = new WriteCoordinator();
  await writeCoordinator.enqueue(() => recoverInterruptedAgentSessions(database, new Date()));
  const registry = createFinancialToolRegistry(database);
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
  const loginService = loginConfig === null ? null : new GoogleLoginService({
    config: loginConfig,
    devicePairingService,
  });
  const orchestrator = new AgentTurnOrchestrator({
    database,
    writeCoordinator,
    registry,
    executor: aiRuntime.executor,
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
      agentAvailability: await aiRuntime.availability(),
      checkedAt: new Date(),
    }),
  });
  registerAgentRoutes(app, {
    orchestrator,
    writeCoordinator,
    database,
    availability: aiRuntime.availability,
    authenticateDevice,
  });
  const eventConnections = await registerAgentEventsRoute(app, { orchestrator, authenticateDevice });
  registerGoogleLoginRoutes(app, {
    loginService,
    pairingService: devicePairingService,
    authenticateDevice,
    onLogout: async (deviceId) => {
      eventConnections.disconnectDevice(deviceId);
      await orchestrator.cancelDeviceTurns(deviceId);
    },
  });
  registerLedgerRoutes(app, {
    registry,
    database,
    authenticateDevice,
  });
  registerAccountRoutes(app, {
    accountService: new AccountService({ database, writeCoordinator }),
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
