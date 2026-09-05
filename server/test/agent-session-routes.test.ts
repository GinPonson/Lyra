import assert from "node:assert/strict";
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import type { FastifyInstance } from "fastify";

import type {
  AgentTurnExecutor,
  AgentTurnStreamSignal,
} from "../src/domain/agent/index.js";
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
import { openLyraDatabase, type LyraDatabase } from "../src/infrastructure/db/database.js";
import { registerAgentRoutes } from "../src/transport/agent-routes.js";
import { buildHttpServer, createServerProtocolMetadata } from "../src/transport/index.js";

const DEVICE_ID = "94000000-0000-4000-8000-000000000001";
const ACCOUNT_ID = "95000000-0000-4000-8000-000000000001";

interface AgentRoutesFixture {
  database: LyraDatabase;
  app: FastifyInstance;
  token: string;
  accountId: string;
  /** 每个已完成回合的 (provider, handoff 是否投递) 记录。 */
  readonly executedTurns: { provider: string; handoffDelivered: boolean }[];
  close: () => Promise<void>;
}

/** 构建带完整种子数据与假执行器的 Agent 路由夹具。 */
async function createAgentRoutesFixture(): Promise<AgentRoutesFixture> {
  const directory = await mkdtemp(join(tmpdir(), "lyra-agent-routes-"));
  const availabilityFixtures = await mkdtemp(join(tmpdir(), "lyra-fake-probe-clis-"));
  const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
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
      CAST($expires_at AS TIMESTAMPTZ), CAST($issued_at AS TIMESTAMPTZ)
    )
  `, {
    id: issued.record.id,
    device_id: DEVICE_ID,
    token_hash: issued.record.tokenHash,
    expires_at: issued.record.expiresAt,
    issued_at: new Date().toISOString(),
  });

  const executedTurns: { provider: string; handoffDelivered: boolean }[] = [];
  const executor: AgentTurnExecutor = async (input) => {
    executedTurns.push({
      provider: input.session.provider,
      handoffDelivered: input.handoffContext !== null,
    });
    const signals: AgentTurnStreamSignal[] = [
      { kind: "message_snapshot", content: "回合完成", stopReason: "end_turn" },
    ];
    for (const signal of signals) {
      input.emitSignal(signal);
    }
    input.emitSignal({ kind: "turn_settled" });
  };
  const fakeCliPath = join(availabilityFixtures, "fake-cli.mjs");
  await writeFile(fakeCliPath, `#!${process.execPath}\nconsole.log("fake-cli 1.0.0");\n`);
  await chmod(fakeCliPath, 0o755);

  const writeCoordinator = new WriteCoordinator();
  const orchestrator = new AgentTurnOrchestrator({
    database,
    writeCoordinator,
    mcpCredentials: new AgentMcpCredentialStore(),
    registry: createFinancialToolRegistry(database),
    executors: { claude: executor, pi: executor, opencode: executor },
    mcpEndpoint: "http://127.0.0.1:3141/mcp",
    mcpCredentialTtlMilliseconds: 60_000,
  });
  const availability = createAgentAvailabilityProvider({
    executables: { claude: fakeCliPath, pi: fakeCliPath, opencode: fakeCliPath },
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

  return {
    database,
    app,
    token: issued.token,
    accountId: ACCOUNT_ID,
    executedTurns,
    close: async () => {
      await app.close();
      await database.close();
      await rm(directory, { recursive: true, force: true });
      await rm(availabilityFixtures, { recursive: true, force: true });
    },
  };
}

describe("Agent session REST routes", () => {
  it("完成认证门禁、会话创建、回合运行、取消与关闭", async () => {
    const fixture = await createAgentRoutesFixture();
    try {
      const headers = {
        authorization: `Bearer ${fixture.token}`,
        "x-lyra-protocol-version": "1",
      };
      const noAuth = await fixture.app.inject({
        method: "GET",
        url: "/v1/agents",
        headers: { "x-lyra-protocol-version": "1" },
      });
      assert.equal(noAuth.statusCode, 401);
      assert.equal(noAuth.json().code, "DEVICE_CREDENTIAL_INVALID");

      const invalidToken = await fixture.app.inject({
        method: "GET",
        url: "/v1/agents",
        headers: {
          authorization: "Bearer not-a-real-token",
          "x-lyra-protocol-version": "1",
        },
      });
      assert.equal(invalidToken.statusCode, 401);

      const agents = await fixture.app.inject({
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

      const invalidBody = await fixture.app.inject({
        method: "POST",
        url: "/v1/agent/sessions",
        headers,
        payload: { provider: "gpt" },
      });
      assert.equal(invalidBody.statusCode, 400);

      const created = await fixture.app.inject({
        method: "POST",
        url: "/v1/agent/sessions",
        headers,
        payload: {
          provider: "opencode",
          accountScope: { kind: "account", account: fixture.accountId },
        },
      });
      assert.equal(created.statusCode, 201);
      const createdSession = created.json().session;
      assert.equal(createdSession.provider, "opencode");
      assert.equal(createdSession.status, "idle");
      assert.equal(createdSession.ledgerVersion, 1);

      const missingAccount = await fixture.app.inject({
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

      const snapshot = await fixture.app.inject({
        method: "GET",
        url: `/v1/agent/sessions/${createdSession.id}`,
        headers,
      });
      assert.equal(snapshot.statusCode, 200);
      assert.equal(snapshot.json().session.status, "idle");

      const turn = await fixture.app.inject({
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

      await fixture.app.inject({
        method: "POST",
        url: `/v1/agent/sessions/${createdSession.id}/messages`,
        headers,
        payload: { content: "长分析" },
      });
      const cancelAfterTurn = await fixture.app.inject({
        method: "POST",
        url: `/v1/agent/sessions/${createdSession.id}/cancel`,
        headers,
      });
      assert.equal(cancelAfterTurn.statusCode, 409);

      const closed = await fixture.app.inject({
        method: "DELETE",
        url: `/v1/agent/sessions/${createdSession.id}`,
        headers,
      });
      assert.equal(closed.statusCode, 200);
      assert.equal(closed.json().session.status, "closed");

      const closedSnapshot = await fixture.app.inject({
        method: "GET",
        url: `/v1/agent/sessions/${createdSession.id}`,
        headers,
      });
      assert.equal(closedSnapshot.json().session.status, "closed");
    } finally {
      await fixture.close();
    }
  });

  it("公开会话工具调用列表并排除内部授权判定", async () => {
    const fixture = await createAgentRoutesFixture();
    try {
      const headers = {
        authorization: `Bearer ${fixture.token}`,
        "x-lyra-protocol-version": "1",
      };
      const created = await fixture.app.inject({
        method: "POST",
        url: "/v1/agent/sessions",
        headers,
        payload: { provider: "claude", accountScope: { kind: "all" } },
      });
      assert.equal(created.statusCode, 201);
      const sessionId = created.json().session.id as string;

      // 跑一个回合产生规范消息，作为工具调用的消息外键。
      const turn = await fixture.app.inject({
        method: "POST",
        url: `/v1/agent/sessions/${sessionId}/messages`,
        headers,
        payload: { content: "查询现金余额" },
      });
      assert.equal(turn.statusCode, 200);
      const messageRows = await fixture.database.connection.runAndReadAll(`
        SELECT CAST(id AS VARCHAR) AS message_id
        FROM agent_messages
        WHERE session_id = CAST($session_id AS UUID)
        ORDER BY ordinal DESC
        LIMIT 1
      `, { session_id: sessionId });
      const messageId = messageRows.getRowObjectsJson()[0]!.message_id as string;

      await fixture.database.connection.run(`
        INSERT INTO agent_tool_calls (
          id, session_id, message_id, ordinal, tool_name, authorization_decision,
          status, arguments_summary, result_summary, ledger_version, duration_ms,
          error_code, started_at, completed_at
        ) VALUES (
          CAST('c1000000-0000-4000-8000-000000000001' AS UUID),
          CAST($session_id AS UUID), CAST($message_id AS UUID), 1,
          'accounts', 'allowed', 'completed',
          CAST('{"accountScope":"all"}' AS JSON),
          CAST('{"count":3}' AS JSON), 1, 42, NULL,
          CAST('2026-09-01T09:00:01Z' AS TIMESTAMPTZ),
          CAST('2026-09-01T09:00:01.042Z' AS TIMESTAMPTZ)
        )
      `, { session_id: sessionId, message_id: messageId });
      await fixture.database.connection.run(`
        INSERT INTO agent_tool_calls (
          id, session_id, message_id, ordinal, tool_name, authorization_decision,
          status, arguments_summary, result_summary, ledger_version, duration_ms,
          error_code, started_at, completed_at
        ) VALUES (
          CAST('c1000000-0000-4000-8000-000000000002' AS UUID),
          CAST($session_id AS UUID), CAST($message_id AS UUID), 2,
          'transactions', 'allowed', 'started',
          CAST('{"accountScope":"all"}' AS JSON),
          NULL, 1, NULL, NULL,
          CAST('2026-09-01T09:00:02Z' AS TIMESTAMPTZ), NULL
        )
      `, { session_id: sessionId, message_id: messageId });

      const listed = await fixture.app.inject({
        method: "GET",
        url: `/v1/agent/sessions/${sessionId}/tool-calls`,
        headers,
      });
      assert.equal(listed.statusCode, 200);
      const toolCalls = listed.json().toolCalls;
      assert.equal(toolCalls.length, 2);
      assert.deepEqual(
        toolCalls.map((call: { toolName: string }) => call.toolName),
        ["accounts", "transactions"],
      );
      const completed = toolCalls[0];
      assert.equal(completed.status, "completed");
      assert.deepEqual(completed.argumentsSummary, { accountScope: "all" });
      assert.deepEqual(completed.resultSummary, { count: 3 });
      assert.equal(completed.durationMs, 42);
      assert.equal(completed.startedAt, "2026-09-01T09:00:01.000Z");
      assert.equal(completed.completedAt, "2026-09-01T09:00:01.042Z");
      assert.equal(completed.errorCode, null);
      // 内部授权判定与会话 ID 由路由上下文确定，不进入公开记录。
      assert.equal("authorizationDecision" in completed, false);
      assert.equal("sessionId" in completed, false);
      const started = toolCalls[1];
      assert.equal(started.status, "started");
      assert.equal(started.resultSummary, null);
      assert.equal(started.completedAt, null);

      const unknown = await fixture.app.inject({
        method: "GET",
        url:
          "/v1/agent/sessions/9f000000-0000-4000-8000-000000000099/tool-calls",
        headers,
      });
      assert.equal(unknown.statusCode, 404);
      assert.equal(unknown.json().code, "AGENT_SESSION_STATE_CONFLICT");
    } finally {
      await fixture.close();
    }
  });

  it("在回合边界切换供应商并持久化边界消息与 handoff", async () => {
    const fixture = await createAgentRoutesFixture();
    try {
      const headers = {
        authorization: `Bearer ${fixture.token}`,
        "x-lyra-protocol-version": "1",
      };
      const created = await fixture.app.inject({
        method: "POST",
        url: "/v1/agent/sessions",
        headers,
        payload: { provider: "opencode", accountScope: { kind: "all" } },
      });
      assert.equal(created.statusCode, 201);
      const sessionId = created.json().session.id as string;

      await fixture.app.inject({
        method: "POST",
        url: `/v1/agent/sessions/${sessionId}/messages`,
        headers,
        payload: { content: "用 opencode 分析" },
      });

      const sameProvider = await fixture.app.inject({
        method: "POST",
        url: `/v1/agent/sessions/${sessionId}/switch`,
        headers,
        payload: { provider: "opencode" },
      });
      assert.equal(sameProvider.statusCode, 400);

      const switched = await fixture.app.inject({
        method: "POST",
        url: `/v1/agent/sessions/${sessionId}/switch`,
        headers,
        payload: { provider: "claude" },
      });
      assert.equal(switched.statusCode, 200);
      const switchedSession = switched.json().session as Record<string, unknown>;
      assert.equal(switchedSession.provider, "claude");
      assert.equal(switchedSession.revision, 4);
      assert.equal(
        (switchedSession.messages as Record<string, unknown>[]).at(-1)?.role,
        "system",
      );

      const turnAfterSwitch = await fixture.app.inject({
        method: "POST",
        url: `/v1/agent/sessions/${sessionId}/messages`,
        headers,
        payload: { content: "继续用 claude 分析" },
      });
      assert.equal(turnAfterSwitch.statusCode, 200);
      assert.equal(
        (turnAfterSwitch.json().events.at(-1)?.payload as Record<string, unknown>)
          ?.provider,
        "claude",
      );

      const persisted = await fixture.database.connection.runAndReadAll(`
        SELECT
          (SELECT provider FROM agent_sessions WHERE id = CAST($session_id AS UUID))
            AS provider,
          (SELECT revision FROM agent_sessions WHERE id = CAST($session_id AS UUID))
            AS revision,
          (SELECT count(*)::INTEGER FROM agent_messages
           WHERE session_id = CAST($session_id AS UUID) AND role = 'system')
            AS boundary_rows
      `, { session_id: sessionId });
      assert.deepEqual(persisted.getRowObjectsJson()[0], {
        provider: "claude",
        // 查询发生在切换后的回合完成之后：switch(+1) → start(+1) → settle(+1)。
        revision: 6,
        boundary_rows: 1,
      });

      assert.deepEqual(fixture.executedTurns, [
        { provider: "opencode", handoffDelivered: false },
        { provider: "claude", handoffDelivered: true },
      ]);
    } finally {
      await fixture.close();
    }
  });
});
