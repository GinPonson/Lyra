import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { WebSocket } from "ws";

import type { AgentTurnExecutor } from "../src/domain/agent/index.js";
import {
  AgentTurnOrchestrator,
  WriteCoordinator,
  createDeviceAuthGate,
  createFinancialToolRegistry,
} from "../src/application/index.js";
import { AgentMcpCredentialStore } from "../src/infrastructure/agents/index.js";
import { issueDeviceAccessToken } from "../src/infrastructure/security/device-auth.js";
import { loadPairedDeviceAuthRecord } from "../src/infrastructure/db/device-access-token-repository.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";
import { registerAgentRoutes } from "../src/transport/agent-routes.js";
import { registerAgentEventsRoute } from "../src/transport/agent-events.js";
import { buildHttpServer, createServerProtocolMetadata } from "../src/transport/index.js";

const DEVICE_ID = "9a000000-0000-4000-8000-000000000001";
const ACCOUNT_ID = "9b000000-0000-4000-8000-000000000001";

interface Waiter {
  predicate: (message: Record<string, unknown>) => boolean;
  resolve: (message: Record<string, unknown>) => void;
  timer: NodeJS.Timeout;
}

/** 收集 WS 消息并支持按谓词等待，防止测试悬挂。 */
function createMessageSink(socket: WebSocket) {
  const received: Record<string, unknown>[] = [];
  const waiters: Waiter[] = [];
  socket.on("message", (data: unknown) => {
    const message = JSON.parse(
      typeof data === "string"
        ? data
        : Buffer.from(data as Uint8Array).toString("utf8"),
    ) as Record<string, unknown>;
    received.push(message);
    for (const [index, waiter] of waiters.entries()) {
      if (waiter.predicate(message)) {
        waiters.splice(index, 1);
        clearTimeout(waiter.timer);
        waiter.resolve(message);
      }
    }
  });
  return {
    received,
    waitFor(predicate: (message: Record<string, unknown>) => boolean): Promise<Record<string, unknown>> {
      const existing = received.find(predicate);
      if (existing !== undefined) {
        return Promise.resolve(existing);
      }
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          reject(new Error("等待 WS 消息超时"));
        }, 5_000);
        waiters.push({ predicate, resolve, timer });
      });
    },
  };
}

describe("Agent events WebSocket surface", () => {
  it("推送实时事件并按游标续传缓冲", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-agent-ws-"));
    const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
    try {
      await database.connection.run(`
        INSERT INTO accounts (
          id, display_name, legacy_account_key, masked_suffix, created_at, updated_at
        ) VALUES (
          CAST($account_id AS UUID), 'Primary', 'SCHWAB-PRIMARY', '1001',
          CAST('2026-09-02T08:00:00Z' AS TIMESTAMPTZ),
          CAST('2026-09-02T08:00:00Z' AS TIMESTAMPTZ)
        )
      `, { account_id: ACCOUNT_ID });
      await database.connection.run(`
        INSERT INTO paired_devices (
          id, display_name, public_key_pem, public_key_fingerprint, key_algorithm,
          paired_at, updated_at
        ) VALUES (
          CAST($device_id AS UUID), 'Android phone', 'public-key', repeat('b', 64),
          'Ed25519', CAST('2026-09-02T08:00:00Z' AS TIMESTAMPTZ),
          CAST('2026-09-02T08:00:00Z' AS TIMESTAMPTZ)
        )
      `, { device_id: DEVICE_ID });
      await database.connection.run(`
        INSERT INTO ledger_versions (reason, source_reference, created_at)
        VALUES ('file_import', 'seed', CAST('2026-09-02T08:30:00Z' AS TIMESTAMPTZ))
      `);
      const device = (await loadPairedDeviceAuthRecord(database, DEVICE_ID))!;
      const issued = issueDeviceAccessToken(device, new Date(), 60_000);
      const issuedAt = new Date().toISOString();
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
        issued_at: issuedAt,
      });

      const writeCoordinator = new WriteCoordinator();
      const executor: AgentTurnExecutor = async (input) => {
        input.emitSignal({ kind: "message_snapshot", content: "回答完成", stopReason: "end_turn" });
        input.emitSignal({ kind: "turn_settled" });
      };
      const orchestrator = new AgentTurnOrchestrator({
        database,
        writeCoordinator,
        mcpCredentials: new AgentMcpCredentialStore(),
        registry: createFinancialToolRegistry(database),
        executors: { claude: executor, pi: executor, opencode: executor },
        mcpEndpoint: "http://127.0.0.1:3141/mcp",
        mcpCredentialTtlMilliseconds: 60_000,
      });
      const app = buildHttpServer({
        protocolMetadata: createServerProtocolMetadata("0.1.0", 11),
        getHealthSnapshot: async () => {
          throw new Error("测试未使用健康端点");
        },
      });
      const authenticateDevice = createDeviceAuthGate(database);
      registerAgentRoutes(app, {
        orchestrator,
        writeCoordinator,
        database,
        availability: async () => ({
          checkedAt: "2026-09-02T09:00:00.000Z",
          providers: [
            { provider: "claude", status: "available", version: "1.0.0" },
            { provider: "pi", status: "available", version: "1.0.0" },
            { provider: "opencode", status: "available", version: "1.0.0" },
          ],
        }),
        authenticateDevice,
      });
      await registerAgentEventsRoute(app, { orchestrator, authenticateDevice });
      await app.listen({ host: "127.0.0.1", port: 0 });
      const address = app.server.address();
      if (address === null || typeof address === "string") {
        throw new Error("测试服务地址无效");
      }
      const wsUrl = `ws://127.0.0.1:${address.port}/v1/events`;
      const headers = {
        authorization: `Bearer ${issued.token}`,
        "x-lyra-protocol-version": "1",
      };

      const created = await app.inject({
        method: "POST",
        url: "/v1/agent/sessions",
        headers,
        payload: { provider: "claude", accountScope: { kind: "all" } },
      });
      assert.equal(created.statusCode, 201);
      const sessionId = created.json().session.id as string;

      const unauthorized = new WebSocket(wsUrl, {
        headers: {
          authorization: "Bearer invalid-token-value",
          "x-lyra-protocol-version": "1",
        },
      });
      const unauthorizedCode = await new Promise<number>((resolve) => {
        unauthorized.on("close", (code) => resolve(code));
      });
      assert.equal(unauthorizedCode, 4001);

      const live = new WebSocket(wsUrl, { headers });
      const liveSink = createMessageSink(live);
      await new Promise((resolve) => live.on("open", resolve));
      live.send(JSON.stringify({ type: "subscribe", sessionId, afterSequence: 0 }));
      const subscribed = await liveSink.waitFor((message) => message.type === "subscribed");
      assert.deepEqual(subscribed, { type: "subscribed", mode: "events", events: [] });

      const turnPromise = app.inject({
        method: "POST",
        url: `/v1/agent/sessions/${sessionId}/messages`,
        headers,
        payload: { content: "总结一下" },
      });
      const started = await liveSink.waitFor((message) => message.type === "event"
        && (message.event as Record<string, unknown>)?.type === "message.started");
      assert.equal((started.event as Record<string, unknown>).sequence, 1);
      const completed = await liveSink.waitFor((message) => message.type === "event"
        && (message.event as Record<string, unknown>)?.type === "message.completed");
      assert.equal((completed.event as Record<string, unknown>).sequence, 2);
      const turn = await turnPromise;
      assert.equal(turn.statusCode, 200);

      const resuming = new WebSocket(wsUrl, { headers });
      const resumeSink = createMessageSink(resuming);
      await new Promise((resolve) => resuming.on("open", resolve));
      resuming.send(JSON.stringify({ type: "subscribe", sessionId, afterSequence: 1 }));
      const resume = await resumeSink.waitFor((message) => message.type === "subscribed");
      assert.equal(resume.mode, "events");
      assert.deepEqual(
        (resume.events as Record<string, unknown>[]).map((event) => event.sequence),
        [2],
      );

      const stale = new WebSocket(wsUrl, { headers });
      const staleSink = createMessageSink(stale);
      await new Promise((resolve) => stale.on("open", resolve));
      stale.send(JSON.stringify({ type: "subscribe", sessionId, afterSequence: 99 }));
      const staleResume = await staleSink.waitFor((message) => message.type === "subscribed");
      assert.equal(staleResume.mode, "snapshot_required");

      const invalidFrame = new WebSocket(wsUrl, { headers });
      const invalidSink = createMessageSink(invalidFrame);
      await new Promise((resolve) => invalidFrame.on("open", resolve));
      invalidFrame.send("not-json");
      const errorFrame = await invalidSink.waitFor((message) => message.type === "error");
      assert.equal(errorFrame.code, "INVALID_ARGUMENT");

      live.close();
      resuming.close();
      stale.close();
      invalidFrame.close();
      await app.close();
    } finally {
      await database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
});
