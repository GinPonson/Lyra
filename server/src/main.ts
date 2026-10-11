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
import { loadOwnerLoginProviders } from "./application/owner-login-providers.js";
import { environmentSchema } from "./infrastructure/config/server-environment.js";
import { createMarketDataRuntimeFromEnvironment } from "./infrastructure/market-data/market-data-runtime.js";
import { MarketSettingsService } from "./application/market-settings-service.js";
import { OwnerLoginService } from "./application/owner-login-service.js";
import { registerLoginRoutes } from "./transport/login-routes.js";
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
import { registerMarketSettingsRoutes } from "./transport/market-settings-routes.js";
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
  // 信号处理器必须早于打开数据库注册：迁移是不可逆的 schema 升级，若在
  // BEGIN…COMMIT 中途被默认动作硬杀，DuckDB 的 schema WAL 可能无法回放
  // （见 database.ts 的迁移说明）。启动阶段收到信号只记录，待配置校验与
  // 迁移完成后立即优雅退出，不打断进行中的事务。
  let pendingSignal: NodeJS.Signals | null = null;
  let shutdown: ((signal: NodeJS.Signals) => Promise<void>) | null = null;
  const handleSignal = (signal: NodeJS.Signals): void => {
    pendingSignal = signal;
    if (shutdown !== null) {
      void shutdown(signal);
    }
  };
  process.on("SIGINT", handleSignal);
  process.on("SIGTERM", handleSignal);

  // 文件系统前提条件同样必须在迁移之前满足：否则一台无法创建暂存目录的
  // 主机仍会把账本库升级到新 schema，而旧服务已无法再打开它。
  mkdirSync(environment.LYRA_IMPORT_STAGING_DIR, { recursive: true });

  // Validate new integration configuration before any irreversible schema upgrade.
  const loginProviders = loadOwnerLoginProviders(process.env);
  const aiRuntime = createGenkitRuntimeFromEnvironment(process.env);
  const marketRuntime = createMarketDataRuntimeFromEnvironment(process.env);
  const marketData = marketRuntime.reader;
  const database = await openLyraDatabase(environment.LYRA_DATABASE_PATH);
  const writeCoordinator = new WriteCoordinator();
  const marketSettingsService = new MarketSettingsService({
    database, writeCoordinator, marketData,
    providers: marketRuntime.providers, defaultRoutes: marketRuntime.defaultRoutes,
  });
  try {
    await writeCoordinator.enqueue(() => recoverInterruptedAgentSessions(database, new Date()));
    await marketSettingsService.initialize();
  } catch (error) {
    await writeCoordinator.close();
    database.close();
    throw error;
  }
  const registry = createFinancialToolRegistry(database, marketData);
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
  const loginService = new OwnerLoginService({
    providers: loginProviders,
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
  registerLoginRoutes(app, {
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
  registerMarketSettingsRoutes(app, { marketSettingsService, authenticateDevice });
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
  const close = async (signal: NodeJS.Signals): Promise<void> => {
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
  shutdown = close;
  // 启动阶段已收到信号：迁移与装配已完成，这里不再监听端口直接退出。
  if (pendingSignal !== null) {
    await close(pendingSignal);
    return;
  }

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
