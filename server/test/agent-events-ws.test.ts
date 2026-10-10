import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { WebSocket } from "ws";

import type { AgentEvent, AgentTurnExecutor } from "../src/domain/agent/index.js";
import {
  AgentTurnOrchestrator,
  WriteCoordinator,
  createDeviceAuthGate,
  createFinancialToolRegistry,
} from "../src/application/index.js";
import { issueDeviceAccessToken, type PairedDeviceAuthRecord } from "../src/infrastructure/security/device-auth.js";
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
        }, 15_000);
        waiters.push({ predicate, resolve, timer });
      });
    },
  };
}

/** 有界等待真实 close 帧；失败时不让 WebSocket 用例无限挂起。 */
function waitForClose(socket: WebSocket): Promise<number> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("等待 WS 关闭超时")), 5_000);
    socket.once("close", (code) => { clearTimeout(timer); resolve(code); });
    socket.once("error", (error) => { clearTimeout(timer); reject(error); });
  });
}

/** ping/pong 保证之前发送的订阅帧已抵达服务端，避免使用时间猜测断言。 */
function waitForRoundTrip(socket: WebSocket): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("等待 WS pong 超时")), 5_000);
    socket.once("pong", () => { clearTimeout(timer); resolve(); });
    socket.ping("auth-test-barrier");
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

/** 用真实 WS 传输、可控认证和受保护事件源隔离认证竞态。 */
async function createAuthRaceFixture() {
  const firstAuthentication = deferred<PairedDeviceAuthRecord>();
  const event: AgentEvent = {
    type: "message.completed", sessionId: ACCOUNT_ID, messageId: null,
    sequence: 1, occurredAt: "2026-10-10T13:00:00.000Z",
    payload: { content: "private financial result" },
  };
  let authenticationCalls = 0;
  let resumeCalls = 0;
  let subscribeCalls = 0;
  const eventSource: Pick<AgentTurnOrchestrator, "resumeEvents" | "subscribe"> = {
    resumeEvents: () => { resumeCalls += 1; return { mode: "events", events: [event] }; },
    subscribe: () => { subscribeCalls += 1; return () => undefined; },
  };
  const app = buildHttpServer({
    protocolMetadata: createServerProtocolMetadata("0.1.0", 13),
    getHealthSnapshot: async () => { throw new Error("unused health endpoint"); },
  });
  await registerAgentEventsRoute(app, {
    // The route only consumes these two event-source methods. No model/database
    // behavior is mocked into the authentication boundary under test.
    orchestrator: eventSource as AgentTurnOrchestrator,
    authenticateDevice: async () => {
      authenticationCalls += 1;
      if (authenticationCalls === 1) return firstAuthentication.promise;
      return { id: DEVICE_ID, status: "active", revokedAt: null };
    },
  });
  await app.listen({ host: "127.0.0.1", port: 0 });
  const address = app.server.address();
  if (address === null || typeof address === "string") throw new Error("测试服务地址无效");
  const socket = new WebSocket(`ws://127.0.0.1:${address.port}/v1/events`, {
    headers: { authorization: "Bearer held-in-app", "x-lyra-protocol-version": "2" },
  });
  const sink = createMessageSink(socket);
  await new Promise<void>((resolve, reject) => {
    socket.once("open", () => resolve());
    socket.once("error", reject);
  });
  return {
    socket, sink, event, firstAuthentication,
    counts: () => ({ authenticationCalls, resumeCalls, subscribeCalls }),
    close: async () => { socket.terminate(); await app.close(); },
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
        registry: createFinancialToolRegistry(database),
        executor,
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
            { provider: "genkit", name: "Lyra", status: "available", version: "Genkit" },
          ],
        }),
        authenticateDevice,
      });
      const connections = await registerAgentEventsRoute(app, { orchestrator, authenticateDevice });
      await app.listen({ host: "127.0.0.1", port: 0 });
      const address = app.server.address();
      if (address === null || typeof address === "string") {
        throw new Error("测试服务地址无效");
      }
      const wsUrl = `ws://127.0.0.1:${address.port}/v1/events`;
      const headers = {
        authorization: `Bearer ${issued.token}`,
        "x-lyra-protocol-version": "2",
      };

      const created = await app.inject({
        method: "POST",
        url: "/v1/agent/sessions",
        headers,
        payload: { provider: "genkit", accountScope: { kind: "all" } },
      });
      assert.equal(created.statusCode, 201);
      const sessionId = created.json().session.id as string;

      const unauthorized = new WebSocket(wsUrl, {
        headers: {
          authorization: "Bearer invalid-token-value",
          "x-lyra-protocol-version": "2",
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

      // 退出登录的连接控制器必须主动关闭该设备的所有已认证连接，而不是等
      // 下一条事件、订阅帧或 token 自然过期。
      const closeCodes = Promise.all([live, resuming, stale, invalidFrame].map(waitForClose));
      connections.disconnectDevice(DEVICE_ID);
      assert.deepEqual(await closeCodes, [4001, 4001, 4001, 4001]);
      await app.close();
    } finally {
      await database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });

  it("订阅未打开运行时的会话返回 snapshot_required 而不中断服务", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-agent-ws-"));
    const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
    try {
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
        registry: createFinancialToolRegistry(database),
        executor,
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
            { provider: "genkit", name: "Lyra", status: "available", version: "Genkit" },
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
      const created = await app.inject({
        method: "POST",
        url: "/v1/agent/sessions",
        headers: {
          authorization: `Bearer ${issued.token}`,
          "x-lyra-protocol-version": "2",
        },
        payload: { provider: "genkit", accountScope: { kind: "all" } },
      });
      assert.equal(created.statusCode, 201);
      const sessionId = created.json().session.id as string;
      await app.close();

      // 第二个服务实例持有独立编排器:该会话的运行时未在内存打开。
      const freshOrchestrator = new AgentTurnOrchestrator({
        database,
        writeCoordinator,
        registry: createFinancialToolRegistry(database),
        executor,
      });
      const eventsApp = buildHttpServer({
        protocolMetadata: createServerProtocolMetadata("0.1.0", 11),
        getHealthSnapshot: async () => {
          throw new Error("测试未使用健康端点");
        },
      });
      await registerAgentEventsRoute(eventsApp, {
        orchestrator: freshOrchestrator,
        authenticateDevice,
      });
      await eventsApp.listen({ host: "127.0.0.1", port: 0 });
      const eventsAddress = eventsApp.server.address();
      if (eventsAddress === null || typeof eventsAddress === "string") {
        throw new Error("测试服务地址无效");
      }

      const socket = new WebSocket(`ws://127.0.0.1:${eventsAddress.port}/v1/events`, {
        headers: {
          authorization: `Bearer ${issued.token}`,
          "x-lyra-protocol-version": "2",
        },
      });
      const sink = createMessageSink(socket);
      await new Promise((resolve) => socket.on("open", resolve));
      socket.send(JSON.stringify({ type: "subscribe", sessionId, afterSequence: 0 }));
      const subscribed = await sink.waitFor((message) => message.type === "subscribed");
      assert.equal(subscribed.mode, "snapshot_required");
      assert.equal(subscribed.reason, "SESSION_NOT_RESUMED");

      socket.close();

      // 进程必须仍然存活:第二条连接同样得到 snapshot_required。
      const second = new WebSocket(`ws://127.0.0.1:${eventsAddress.port}/v1/events`, {
        headers: {
          authorization: `Bearer ${issued.token}`,
          "x-lyra-protocol-version": "2",
        },
      });
      const secondSink = createMessageSink(second);
      await new Promise((resolve) => second.on("open", resolve));
      second.send(JSON.stringify({ type: "subscribe", sessionId, afterSequence: 0 }));
      const repeat = await secondSink.waitFor((message) => message.type === "subscribed");
      assert.equal(repeat.mode, "snapshot_required");
      second.close();
      await eventsApp.close();
    } finally {
      await database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });

  it("首帧等待延迟认证完成后才读取和返回受保护事件", async () => {
    const f = await createAuthRaceFixture();
    try {
      f.socket.send(JSON.stringify({ type: "subscribe", sessionId: ACCOUNT_ID, afterSequence: 0 }));
      await waitForRoundTrip(f.socket);
      assert.deepEqual(f.counts(), { authenticationCalls: 1, resumeCalls: 0, subscribeCalls: 0 });
      assert.deepEqual(f.sink.received, []);
      f.firstAuthentication.resolve({ id: DEVICE_ID, status: "active", revokedAt: null });
      const subscribed = await f.sink.waitFor((message) => message.type === "subscribed");
      assert.deepEqual(subscribed, { type: "subscribed", mode: "events", events: [f.event] });
      assert.deepEqual(f.counts(), { authenticationCalls: 2, resumeCalls: 1, subscribeCalls: 1 });
    } finally { await f.close(); }
  });

  it("延迟首轮认证失败时关闭连接且不暴露已排队订阅的事件", async () => {
    const f = await createAuthRaceFixture();
    try {
      f.socket.send(JSON.stringify({ type: "subscribe", sessionId: ACCOUNT_ID, afterSequence: 0 }));
      await waitForRoundTrip(f.socket);
      assert.deepEqual(f.counts(), { authenticationCalls: 1, resumeCalls: 0, subscribeCalls: 0 });
      const closed = waitForClose(f.socket);
      f.firstAuthentication.reject(new Error("invalid or revoked credential"));
      assert.equal(await closed, 4001);
      assert.deepEqual(f.sink.received, []);
      assert.deepEqual(f.counts(), { authenticationCalls: 1, resumeCalls: 0, subscribeCalls: 0 });
    } finally { await f.close(); }
  });
});
