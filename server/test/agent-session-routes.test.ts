import assert from "node:assert/strict";
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import type { AgentTurnExecutor } from "../src/domain/agent/index.js";
import {
  AgentTurnOrchestrator,
  WriteCoordinator,
  createAgentAvailabilityProvider,
  createDeviceAuthGate,
  createFinancialToolRegistry,
} from "../src/application/index.js";
import { AgentMcpCredentialStore } from "../src/infrastructure/agents/index.js";
import { issueDeviceAccessToken } from "../src/infrastructure/security/device-auth.js";
import { loadPairedDeviceAuthRecord } from "../src/infrastructure/db/device-access-token-repository.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";
import { registerAgentRoutes } from "../src/transport/agent-routes.js";
import { buildHttpServer, createServerProtocolMetadata } from "../src/transport/index.js";

const DEVICE_ID = "94000000-0000-4000-8000-000000000001";
const ACCOUNT_ID = "95000000-0000-4000-8000-000000000001";

describe("Agent session REST routes", () => {
  it("完成认证门禁、会话创建、回合运行、取消与关闭", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-agent-routes-"));
    let availabilityFixtures: string | null = null;
    const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
    try {
      await database.connection.run(`
        INSERT INTO accounts (
          id, display_name, legacy_account_key, masked_suffix, created_at, updated_at
        ) VALUES (
          CAST($account_id AS UUID), 'Primary', 'SCHWAB-PRIMARY', '1001',
          CAST('2026-09-01T08:00:00Z' AS TIMESTAMPTZ),
          CAST('2026-09-01T08:00:00Z' AS TIMESTAMPTZ)
        )
      `, { account_id: ACCOUNT_ID });
      await database.connection.run(`
        INSERT INTO paired_devices (
          id, display_name, public_key_pem, public_key_fingerprint, key_algorithm,
          paired_at, updated_at
        ) VALUES (
          CAST($device_id AS UUID), 'Android phone', 'public-key', repeat('a', 64),
          'Ed25519', CAST('2026-09-01T08:00:00Z' AS TIMESTAMPTZ),
          CAST('2026-09-01T08:00:00Z' AS TIMESTAMPTZ)
        )
      `, { device_id: DEVICE_ID });
      await database.connection.run(`
        INSERT INTO ledger_versions (reason, source_reference, created_at)
        VALUES ('file_import', 'seed', CAST('2026-09-01T08:30:00Z' AS TIMESTAMPTZ))
      `);

      const device = (await loadPairedDeviceAuthRecord(database, DEVICE_ID))!;
      const issued = issueDeviceAccessToken(device, new Date(), 60_000);
      await database.connection.run(`
        INSERT INTO device_access_tokens (
          id, device_id, token_hash, status, expires_at, issued_at
        ) VALUES (
          CAST($id AS UUID), CAST($device_id AS UUID), $token_hash, 'active',
          CAST($expires_at AS TIMESTAMPTZ), CAST('2026-09-01T09:00:00Z' AS TIMESTAMPTZ)
        )
      `, {
        id: issued.record.id,
        device_id: DEVICE_ID,
        token_hash: issued.record.tokenHash,
        expires_at: issued.record.expiresAt,
      });

      const writeCoordinator = new WriteCoordinator();
      const mcpCredentials = new AgentMcpCredentialStore();
      const executor: AgentTurnExecutor = async (input) => {
        input.emitSignal({ kind: "message_snapshot", content: "回合完成", stopReason: "end_turn" });
        input.emitSignal({ kind: "turn_settled" });
      };
      const orchestrator = new AgentTurnOrchestrator({
        database,
        writeCoordinator,
        mcpCredentials,
        registry: createFinancialToolRegistry(database),
        executors: {
          claude: executor,
          pi: executor,
          opencode: executor,
        },
        mcpEndpoint: "http://127.0.0.1:3141/mcp",
        mcpCredentialTtlMilliseconds: 60_000,
      });
      const availabilityFixturesDirectory = await mkdtemp(join(tmpdir(), "lyra-fake-probe-clis-"));
      availabilityFixtures = availabilityFixturesDirectory;
      const fakeCliPath = join(availabilityFixturesDirectory, "fake-cli.mjs");
      await writeFile(fakeCliPath, `#!${process.execPath}\nconsole.log("fake-cli 1.0.0");\n`);
      await chmod(fakeCliPath, 0o755);
      const availability = createAgentAvailabilityProvider({
        executables: {
          claude: fakeCliPath,
          pi: fakeCliPath,
          opencode: fakeCliPath,
        },
        allowedVersions: { claude: ["1.0.0"], pi: ["1.0.0"], opencode: ["1.0.0"] },
        probeTimeoutMilliseconds: 5_000,
        cacheMilliseconds: 60_000,
        now: () => new Date("2026-09-01T09:00:00.000Z"),
      });
      const app = buildHttpServer({
        protocolMetadata: createServerProtocolMetadata("0.1.0", 11),
        getHealthSnapshot: async () => {
          throw new Error("测试未使用健康端点");
        },
      });
      registerAgentRoutes(app, {
        orchestrator,
        writeCoordinator,
        database,
        availability,
        authenticateDevice: createDeviceAuthGate(database),
      });

      const headers = {
        authorization: `Bearer ${issued.token}`,
        "x-lyra-protocol-version": "1",
      };
      const noAuth = await app.inject({
        method: "GET",
        url: "/v1/agents",
        headers: { "x-lyra-protocol-version": "1" },
      });
      assert.equal(noAuth.statusCode, 401);
      assert.equal(noAuth.json().code, "DEVICE_CREDENTIAL_INVALID");

      const invalidToken = await app.inject({
        method: "GET",
        url: "/v1/agents",
        headers: {
          authorization: "Bearer not-a-real-token",
          "x-lyra-protocol-version": "1",
        },
      });
      assert.equal(invalidToken.statusCode, 401);

      const agents = await app.inject({
        method: "GET",
        url: "/v1/agents",
        headers,
      });
      assert.equal(agents.statusCode, 200);
      assert.deepEqual(agents.json().providers, [
        { provider: "claude", status: "available", version: "1.0.0" },
        { provider: "pi", status: "available", version: "1.0.0" },
        { provider: "opencode", status: "available", version: "1.0.0" },
      ]);

      const invalidBody = await app.inject({
        method: "POST",
        url: "/v1/agent/sessions",
        headers,
        payload: { provider: "gpt" },
      });
      assert.equal(invalidBody.statusCode, 400);

      const created = await app.inject({
        method: "POST",
        url: "/v1/agent/sessions",
        headers,
        payload: {
          provider: "opencode",
          accountScope: { kind: "account", account: ACCOUNT_ID },
        },
      });
      assert.equal(created.statusCode, 201);
      const createdSession = created.json().session;
      assert.equal(createdSession.provider, "opencode");
      assert.equal(createdSession.status, "idle");
      assert.equal(createdSession.ledgerVersion, 1);

      const missingAccount = await app.inject({
        method: "POST",
        url: "/v1/agent/sessions",
        headers,
        payload: {
          provider: "opencode",
          accountScope: {
            kind: "account",
            account: "99000000-0000-4000-8000-000000000001",
          },
        },
      });
      assert.equal(missingAccount.statusCode, 409);
      assert.equal(missingAccount.json().code, "ACCOUNT_SCOPE_REQUIRED");

      const snapshot = await app.inject({
        method: "GET",
        url: `/v1/agent/sessions/${createdSession.id}`,
        headers,
      });
      assert.equal(snapshot.statusCode, 200);
      assert.equal(snapshot.json().session.status, "idle");

      const turn = await app.inject({
        method: "POST",
        url: `/v1/agent/sessions/${createdSession.id}/messages`,
        headers,
        payload: { content: " 分析当前持仓 " },
      });
      assert.equal(turn.statusCode, 200);
      assert.equal(turn.json().session.status, "idle");
      assert.equal(turn.json().session.revision, 3);
      assert.deepEqual(turn.json().events.map((event: { type: string }) => event.type), [
        "message.started",
        "message.completed",
      ]);
      assert.equal(
        turn.json().events.at(-1)?.payload?.provider,
        "opencode",
      );

      await app.inject({
        method: "POST",
        url: `/v1/agent/sessions/${createdSession.id}/messages`,
        headers,
        payload: { content: "长分析" },
      });
      const cancelDuringTurn = await app.inject({
        method: "POST",
        url: `/v1/agent/sessions/${createdSession.id}/cancel`,
        headers,
      });
      assert.equal(cancelDuringTurn.statusCode, 409);

      const closed = await app.inject({
        method: "DELETE",
        url: `/v1/agent/sessions/${createdSession.id}`,
        headers,
      });
      assert.equal(closed.statusCode, 200);
      assert.equal(closed.json().session.status, "closed");

      const closedSnapshot = await app.inject({
        method: "GET",
        url: `/v1/agent/sessions/${createdSession.id}`,
        headers,
      });
      assert.equal(closedSnapshot.json().session.status, "closed");

      await app.close();
    } finally {
      await database.close();
      await rm(directory, { recursive: true, force: true });
      if (availabilityFixtures !== null) {
        await rm(availabilityFixtures, { recursive: true, force: true });
      }
    }
  });
});
