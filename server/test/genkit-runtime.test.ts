import assert from "node:assert/strict";
import { createServer } from "node:http";
import { setImmediate as nextTick } from "node:timers/promises";
import { describe, it } from "node:test";

import { genkit } from "genkit";
import type { GenerateRequest, GenerateResponseData } from "genkit";

import { AgentTurnOrchestrator } from "../src/application/agent-turn-orchestrator.js";
import { WriteCoordinator } from "../src/application/write-coordinator.js";
import { createAgentSession, FinancialToolRegistry } from "../src/domain/agent/index.js";
import type { AgentToolInvocation, AgentTurnExecutor, AgentTurnStreamSignal } from "../src/domain/agent/index.js";
import { createGenkitRuntimeFromEnvironment, createGenkitTurnExecutor } from "../src/infrastructure/agents/genkit-runtime.js";
import { loadAgentSession, persistNewAgentSession } from "../src/infrastructure/db/agent-session-repository.js";
import { loadAgentToolCalls } from "../src/infrastructure/db/agent-tool-call-repository.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";

const ACCOUNT_ID = "ea000000-0000-4000-8000-000000000001";
const DEVICE_ID = "eb000000-0000-4000-8000-000000000001";
const SESSION_ID = "ec000000-0000-4000-8000-000000000001";
const AS_OF = "2026-10-10T13:00:00.000Z";
const SUPPORTS = { multiturn: true, tools: true, systemRole: true } as const;

function answer(text: string): GenerateResponseData {
  return { message: { role: "model", content: [{ text }] }, finishReason: "stop", usage: { totalTokens: 7 } };
}

/** Real Genkit plus real DuckDB; only the billable model and read-query data are fake. */
async function fixture(registry: FinancialToolRegistry, executor: AgentTurnExecutor, writeCoordinator = new WriteCoordinator()) {
  const database = await openLyraDatabase(":memory:");
  await database.connection.run(`
    INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
    VALUES (CAST($account_id AS UUID), 'Primary', 'TEST-PRIMARY', '0001')
  `, { account_id: ACCOUNT_ID });
  await database.connection.run(`
    INSERT INTO paired_devices (
      id, display_name, public_key_pem, public_key_fingerprint, key_algorithm,
      paired_at, updated_at
    ) VALUES (CAST($device_id AS UUID), 'Phone', 'test-key', repeat('b', 64),
      'Ed25519', CAST($now AS TIMESTAMPTZ), CAST($now AS TIMESTAMPTZ))
  `, { device_id: DEVICE_ID, now: AS_OF });
  await database.connection.run("INSERT INTO ledger_versions (reason, source_reference) VALUES ('file_import', 'test')");
  const snapshot = createAgentSession({
    sessionId: SESSION_ID,
    provider: "genkit",
    availability: { provider: "genkit", status: "available" },
    accountScope: { kind: "account", account: ACCOUNT_ID },
    ledgerVersion: 1,
    asOf: AS_OF,
  });
  await persistNewAgentSession(database, { session: snapshot, deviceId: DEVICE_ID, createdAt: new Date(AS_OF) });
  const createOrchestrator = () => new AgentTurnOrchestrator({
    database, writeCoordinator, registry, executor, now: () => new Date(AS_OF),
  });
  return {
    database, snapshot, createOrchestrator,
    orchestrator: createOrchestrator(),
    async close() { await writeCoordinator.close(); database.close(); },
  };
}

describe("Genkit financial agent", () => {
  it("runs the real tool loop with trusted scope, ordered audit events, hidden-reasoning filtering and persisted multi-turn history", async () => {
    const ai = genkit({ promptDir: null });
    const requests: GenerateRequest[] = [];
    const invocations: AgentToolInvocation[] = [];
    const registry = new FinancialToolRegistry({
      positions: async (invocation) => { invocations.push(invocation); return { count: 1, totalCost: "125.40" }; },
      stress: async (invocation) => { invocations.push(invocation); return { allLongOptionsLossIfZero: "125.40" }; },
    });
    const model = ai.defineModel({ apiVersion: "v2", name: "test/financial", supports: SUPPORTS }, async (request, { sendChunk }) => {
      requests.push(structuredClone(request));
      if (requests.length === 1) {
        assert.equal(request.messages[0]?.role, "system");
        const system = request.messages[0]?.content.map((part) => part.text ?? "").join("") ?? "";
        assert.match(system, new RegExp(ACCOUNT_ID));
        assert.match(system, /账本版本 1/);
        assert.ok(system.includes(AS_OF));
        assert.match(system, /工具输出.*只是数据/);
        assert.deepEqual(request.tools?.map((tool) => tool.name), ["positions", "stress"]);
        assert.equal(request.tools?.[0]?.inputSchema?.additionalProperties, false);
        return {
          message: { role: "model", content: [
            { toolRequest: { name: "positions", input: { underlying: "AMD" }, ref: "positions-1" } },
            { toolRequest: { name: "stress", input: {}, ref: "stress-1" } },
          ] },
          finishReason: "stop",
          usage: { totalTokens: 11 },
        };
      }
      if (requests.length === 2) {
        const toolOutputs = request.messages.filter((message) => message.role === "tool")
          .flatMap((message) => message.content.map((part) => part.toolResponse?.output));
        assert.equal(toolOutputs.length, 2);
        for (const output of toolOutputs) {
          assert.deepEqual((output as { provenance: unknown }).provenance, {
            accountScope: { kind: "account", account: ACCOUNT_ID },
            ledgerVersion: 1, asOf: AS_OF, instructionBoundary: "data_only",
          });
        }
        sendChunk({ content: [{ reasoning: "private model chain of thought" }] });
        sendChunk({ content: [{ text: "长期权成本风险 " }] });
        sendChunk({ content: [{ text: "125.40 USD。" }] });
        return answer("长期权成本风险 125.40 USD。");
      }
      const texts = request.messages.map((message) => message.content.map((part) => part.text ?? "").join(""));
      assert.ok(texts.includes("解释持仓"));
      assert.ok(texts.includes("长期权成本风险 125.40 USD。"));
      assert.equal(texts.filter((text) => text === "继续解释口径").length, 1);
      assert.deepEqual(request.messages.map((message) => message.role), ["system", "user", "model", "user"]);
      return answer("继续解释此前使用的历史成本口径。");
    });
    const f = await fixture(registry, createGenkitTurnExecutor({ ai, model }));
    try {
      const run = await f.orchestrator.startTurn(SESSION_ID, "解释持仓");
      assert.equal(run.snapshot.status, "idle");
      assert.equal(run.snapshot.messages.at(-1)?.content, "长期权成本风险 125.40 USD。");
      assert.equal(run.events.at(-1)?.type, "message.completed");
      assert.deepEqual(run.events.map((event) => event.sequence), run.events.map((_, index) => index + 1));
      assert.deepEqual(run.events.filter((event) => event.type.startsWith("tool.")).map((event) => event.type), [
        "tool.started", "tool.completed", "tool.started", "tool.completed",
      ]);
      const usage = run.events.find((event) => event.type === "usage.updated");
      assert.equal(usage?.payload.total_tokens, 18);
      assert.equal(usage?.payload.total_cost, null);
      assert.ok(!JSON.stringify(run).includes("private model chain of thought"));
      assert.deepEqual(invocations.map((invocation) => invocation.context), [
        { accountScope: { kind: "account", account: ACCOUNT_ID }, ledgerVersion: 1, asOf: AS_OF },
        { accountScope: { kind: "account", account: ACCOUNT_ID }, ledgerVersion: 1, asOf: AS_OF },
      ]);
      const calls = await loadAgentToolCalls(f.database, SESSION_ID);
      assert.deepEqual(calls?.map((call) => [call.toolName, call.status]), [["positions", "completed"], ["stress", "completed"]]);

      // A fresh orchestrator must restore visible history without a provider cache.
      const resumed = f.createOrchestrator();
      const second = await resumed.startTurn(SESSION_ID, "继续解释口径");
      assert.equal(second.events.at(-1)?.type, "message.completed");
      assert.equal((await loadAgentSession(f.database, SESSION_ID))?.messages.length, 4);
    } finally {
      await f.close();
      await ai.stopServers();
    }
  });

  it("rejects unknown tools, injected scope and malformed arguments before any handler and persists each denial", async () => {
    const ai = genkit({ promptDir: null });
    const attacks = [
      { name: "run-shell", input: { command: "secret command must not be stored" } },
      { name: "positions", input: { accountScope: { kind: "all" }, ledgerVersion: 999 } },
      { name: "expiring", input: { days: "7" } },
      { name: "trades", input: { fromDate: "2026-02-30" } },
    ];
    let attempted = 0;
    let executions = 0;
    const model = ai.defineModel({ apiVersion: "v2", name: "test/rejections", supports: SUPPORTS }, async () => ({
      message: { role: "model", content: [{ toolRequest: { ...attacks[attempted++]!, ref: "attack" } }] },
      finishReason: "stop",
    }));
    const handler = async () => { executions += 1; return {}; };
    const f = await fixture(new FinancialToolRegistry({ positions: handler, expiring: handler, trades: handler }), createGenkitTurnExecutor({ ai, model }));
    try {
      for (const _attack of attacks) {
        const run = await f.orchestrator.startTurn(SESSION_ID, "尝试工具");
        assert.equal(run.events.at(-1)?.type, "message.failed");
        assert.equal(run.events.find((event) => event.type === "tool.completed")?.payload.outcome, "rejected");
      }
      assert.equal(executions, 0);
      const calls = await loadAgentToolCalls(f.database, SESSION_ID);
      assert.equal(calls?.length, attacks.length);
      assert.ok(calls?.every((call) => call.status === "rejected" && Object.keys(call.argumentsSummary).length === 0));
      assert.ok(!JSON.stringify(calls).includes("secret command"));
      const audit = await f.database.connection.runAndReadAll("SELECT count(*)::INTEGER AS count FROM audit_events WHERE event_type = 'agent.tool_rejected'");
      assert.equal(audit.getRowObjectsJson()[0]?.count, attacks.length);
    } finally {
      await f.close();
      await ai.stopServers();
    }
  });

  it("cancels a real Genkit tool round on logout, settles its audit and suppresses late results", async () => {
    const ai = genkit({ promptDir: null });
    const entered = deferred<void>();
    const release = deferred<void>();
    let modelCalls = 0;
    const model = ai.defineModel({ apiVersion: "v2", name: "test/cancel", supports: SUPPORTS }, async () => {
      modelCalls += 1;
      return { message: { role: "model", content: [{ toolRequest: { name: "stress", input: {}, ref: "stress" } }] }, finishReason: "stop" };
    });
    const registry = new FinancialToolRegistry({ stress: async () => { entered.resolve(); await release.promise; return { count: 1 }; } });
    const f = await fixture(registry, createGenkitTurnExecutor({ ai, model }));
    try {
      const runPromise = f.orchestrator.startTurn(SESSION_ID, "压力分析");
      await entered.promise;
      await f.orchestrator.cancelDeviceTurns(DEVICE_ID);
      const run = await runPromise;
      assert.equal(run.snapshot.status, "idle");
      assert.equal(run.events.at(-1)?.payload.error_code, "AGENT_TURN_CANCELLED");
      assert.equal((await loadAgentToolCalls(f.database, SESSION_ID))?.[0]?.status, "cancelled");
      const eventCount = run.events.length;
      release.resolve();
      await nextTick();
      assert.equal(run.events.length, eventCount);
      assert.equal(modelCalls, 1);
    } finally {
      release.resolve();
      await f.close();
      await ai.stopServers();
    }
  });

  it("does not start the model when logout crosses the turn-start persistence await", async () => {
    const persisted = deferred<void>();
    const release = deferred<void>();
    class HeldStartCoordinator extends WriteCoordinator {
      #first = true;
      override async enqueue<T>(operation: () => Promise<T>): Promise<T> {
        const result = await super.enqueue(operation);
        if (this.#first) {
          this.#first = false;
          persisted.resolve();
          await release.promise;
        }
        return result;
      }
    }
    let modelCalls = 0;
    const executor: AgentTurnExecutor = async () => { modelCalls += 1; };
    const f = await fixture(new FinancialToolRegistry({}), executor, new HeldStartCoordinator());
    try {
      const runPromise = f.orchestrator.startTurn(SESSION_ID, "启动期间退出");
      await persisted.promise;
      await f.orchestrator.cancelDeviceTurns(DEVICE_ID);
      release.resolve();
      const run = await runPromise;
      assert.equal(modelCalls, 0);
      assert.equal(run.snapshot.status, "idle");
      assert.equal(run.events.at(-1)?.payload.error_code, "AGENT_TURN_CANCELLED");
      assert.equal((await loadAgentSession(f.database, SESSION_ID))?.messages.length, 1);
    } finally {
      release.resolve();
      await f.close();
    }
  });

  it("waits for pending cancellation persistence when the model settles concurrently", async () => {
    const entered = deferred<void>();
    const releaseModel = deferred<void>();
    const cancellationPersisted = deferred<void>();
    const releaseCancellation = deferred<void>();
    const modelSettled = deferred<void>();
    let abortedAtSettlement = false;
    class HeldCancellationCoordinator extends WriteCoordinator {
      #operations = 0;
      override async enqueue<T>(operation: () => Promise<T>): Promise<T> {
        const operationNumber = ++this.#operations;
        const result = await super.enqueue(operation);
        if (operationNumber === 2) {
          cancellationPersisted.resolve();
          await releaseCancellation.promise;
        }
        return result;
      }
    }
    const executor: AgentTurnExecutor = async ({ emitSignal, abortSignal }) => {
      entered.resolve();
      await releaseModel.promise;
      abortedAtSettlement = abortSignal.aborted;
      emitSignal({ kind: "message_snapshot", content: "迟到的模型结果", stopReason: "end_turn" });
      emitSignal({ kind: "turn_settled" });
      modelSettled.resolve();
    };
    const f = await fixture(new FinancialToolRegistry({}), executor, new HeldCancellationCoordinator());
    try {
      let finalized = false;
      const runPromise = f.orchestrator.startTurn(SESSION_ID, "取消和模型结束同时发生");
      void runPromise.then(() => { finalized = true; }, () => { finalized = true; });
      await entered.promise;
      const cancellation = f.orchestrator.cancelTurn(SESSION_ID);
      await cancellationPersisted.promise;
      const duplicateCancellation = f.orchestrator.cancelTurn(SESSION_ID);
      releaseModel.resolve();
      await modelSettled.promise;
      await nextTick();
      assert.equal(abortedAtSettlement, true);
      assert.equal(finalized, false);
      releaseCancellation.resolve();
      assert.equal((await cancellation).status, "cancelling");
      assert.equal((await duplicateCancellation).status, "cancelling");
      const run = await runPromise;
      assert.equal(run.snapshot.status, "idle");
      assert.equal(run.events.at(-1)?.payload.error_code, "AGENT_TURN_CANCELLED");
      assert.ok(!run.snapshot.messages.some((message) => message.content === "迟到的模型结果"));
      assert.equal((await loadAgentSession(f.database, SESSION_ID))?.status, "idle");
    } finally {
      releaseModel.resolve();
      releaseCancellation.resolve();
      await f.close();
    }
  });

  it("ends a stalled model at its deadline and bounds an endless tool loop", async () => {
    const ai = genkit({ promptDir: null });
    const release = deferred<GenerateResponseData>();
    const model = ai.defineModel({ apiVersion: "v2", name: "test/deadline", supports: SUPPORTS }, async () => release.promise);
    const f = await fixture(new FinancialToolRegistry({}), createGenkitTurnExecutor({ ai, model, turnTimeoutMilliseconds: 100 }));
    try {
      const run = await f.orchestrator.startTurn(SESSION_ID, "等待");
      assert.equal(run.snapshot.status, "idle");
      assert.equal(run.events.at(-1)?.payload.error_code, "AGENT_TURN_FAILED");
      const events = run.events.length;
      release.resolve(answer("迟到的结果"));
      await nextTick();
      assert.equal(run.events.length, events);
    } finally {
      release.resolve(answer("结束"));
      await f.close();
      await ai.stopServers();
    }

    const loopAI = genkit({ promptDir: null });
    const looping = loopAI.defineModel({ apiVersion: "v2", name: "test/loop", supports: SUPPORTS }, async () => ({
      message: { role: "model", content: [{ toolRequest: { name: "stress", input: {}, ref: "again" } }] }, finishReason: "stop",
    }));
    const bounded = await fixture(new FinancialToolRegistry({ stress: async () => ({ count: 0 }) }), createGenkitTurnExecutor({ ai: loopAI, model: looping, maxTurns: 1 }));
    try {
      const run = await bounded.orchestrator.startTurn(SESSION_ID, "循环");
      assert.equal(run.events.at(-1)?.payload.error_code, "AGENT_TURN_FAILED");
      assert.equal((await loadAgentToolCalls(bounded.database, SESSION_ID))?.length, 1);
    } finally {
      await bounded.close();
      await loopAI.stopServers();
    }
  });

  it("uses the official compatible plugin over HTTP streaming with one configured backend", async () => {
    const requests: Record<string, unknown>[] = [];
    const endpoint = createServer(async (request, response) => {
      const chunks: Buffer[] = [];
      for await (const chunk of request) chunks.push(Buffer.from(chunk));
      requests.push(JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>);
      assert.equal(request.url, "/v1/chat/completions");
      assert.equal(request.headers.authorization, "Bearer local-test-only");
      response.writeHead(200, { "content-type": "text/event-stream" });
      const delta = requests.length === 1
        ? { role: "assistant", tool_calls: [{ index: 0, id: "call_1", type: "function", function: { name: "stress", arguments: "{}" } }] }
        : { role: "assistant", content: "已读取压力分析工具结果。" };
      response.write(`data: ${JSON.stringify({ id: "chat_test", object: "chat.completion.chunk", created: 1, model: "custom/model", choices: [{ index: 0, delta, finish_reason: null }] })}\n\n`);
      response.write(`data: ${JSON.stringify({ id: "chat_test", object: "chat.completion.chunk", created: 1, model: "custom/model", choices: [{ index: 0, delta: {}, finish_reason: requests.length === 1 ? "tool_calls" : "stop" }], usage: { prompt_tokens: 5, completion_tokens: 2, total_tokens: 7 } })}\n\n`);
      response.end("data: [DONE]\n\n");
    });
    await new Promise<void>((resolve) => endpoint.listen(0, "127.0.0.1", resolve));
    const address = endpoint.address();
    assert.ok(address && typeof address !== "string");
    const runtime = createGenkitRuntimeFromEnvironment({
      LYRA_GENKIT_BASE_URL: `http://127.0.0.1:${address.port}/v1`,
      LYRA_GENKIT_MODEL: "custom/model", LYRA_GENKIT_API_KEY: "local-test-only",
    });
    const f = await fixture(new FinancialToolRegistry({ stress: async () => ({ allLongOptionsLossIfZero: "5.50" }) }), runtime.executor);
    try {
      const run = await f.orchestrator.startTurn(SESSION_ID, "压力分析");
      assert.equal(run.events.at(-1)?.type, "message.completed");
      assert.equal(requests.length, 2);
      assert.equal(requests[0]?.model, "custom/model");
      assert.equal(requests[0]?.stream, true);
      assert.ok(JSON.stringify(requests[1]?.messages).includes("lyra_financial_data"));
      const available = await runtime.availability();
      assert.equal(available.providers.length, 1);
      assert.ok(!JSON.stringify(available).includes("local-test-only"));
      assert.ok(!JSON.stringify(available).includes(String(address.port)));
    } finally {
      await f.close();
      endpoint.closeAllConnections();
      await new Promise<void>((resolve, reject) => endpoint.close((error) => error ? reject(error) : resolve()));
    }
  });

  it("keeps an unconfigured deployment unavailable without guessing a provider or endpoint", async () => {
    const runtime = createGenkitRuntimeFromEnvironment({});
    assert.equal((await runtime.availability()).providers[0]?.status, "unavailable");
    const signals: AgentTurnStreamSignal[] = [];
    await assert.rejects(runtime.executor({
      session: { id: "test", provider: "genkit", status: "running", accountScope: { kind: "all" }, ledgerVersion: 1, asOf: AS_OF, revision: 1, messages: [] },
      adapterInput: { messageId: "test", content: "hi" }, toolDefinitions: [],
      executeTool: async () => { throw new Error("must not execute"); },
      emitSignal: (signal) => signals.push(signal), abortSignal: new AbortController().signal,
    }));
    assert.deepEqual(signals, []);
  });
});

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => { resolve = settle; });
  return { promise, resolve };
}
