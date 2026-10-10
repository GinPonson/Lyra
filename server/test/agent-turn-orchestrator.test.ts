import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import type { AgentTurnExecutor } from "../src/application/index.js";
import {
  AgentTurnOrchestrator,
  WriteCoordinator,
  createFinancialToolRegistry,
} from "../src/application/index.js";
import { createAgentSession } from "../src/domain/agent/index.js";
import { persistNewAgentSession } from "../src/infrastructure/db/agent-session-repository.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";

const ACCOUNT_ID = "81000000-0000-4000-8000-000000000001";
const DEVICE_ID = "82000000-0000-4000-8000-000000000001";
const SESSION_ID = "83000000-0000-4000-8000-000000000001";
const CANCEL_SESSION_ID = "83000000-0000-4000-8000-000000000002";

/** 写入满足外键的最小账户、设备和账本版本。 */
async function seedLedger(databasePath: string): Promise<void> {
  const database = await openLyraDatabase(databasePath);
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
        CAST($device_id AS UUID), 'Android phone', 'public-key', repeat('d', 64),
        'Ed25519', CAST('2026-09-01T08:00:00Z' AS TIMESTAMPTZ),
        CAST('2026-09-01T08:00:00Z' AS TIMESTAMPTZ)
      )
    `, { device_id: DEVICE_ID });
    await database.connection.run(`
      INSERT INTO ledger_versions (reason, source_reference, created_at)
      VALUES ('file_import', 'seed', CAST('2026-09-01T08:30:00Z' AS TIMESTAMPTZ))
    `);
  } finally {
    database.close();
  }
}

describe("Agent turn orchestrator", () => {
  it("编排成功与失败回合、工具调用持久化和事件序号", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-orchestrator-"));
    const databasePath = join(directory, "ledger.duckdb");
    try {
      await seedLedger(databasePath);
      const database = await openLyraDatabase(databasePath);
      const session = createAgentSession({
        sessionId: SESSION_ID,
        provider: "genkit",
        availability: { provider: "genkit", status: "available" },
        accountScope: { kind: "account", account: ACCOUNT_ID },
        ledgerVersion: 1,
        asOf: "2026-09-01T09:00:00.000Z",
      });
      await persistNewAgentSession(database, {
        session,
        deviceId: DEVICE_ID,
        createdAt: new Date("2026-09-01T09:00:00.000Z"),
      });

      let observedContent: string | null = null;
      const successExecutor: AgentTurnExecutor = async (input) => {
        observedContent = input.adapterInput.content;
        input.emitSignal({ kind: "reasoning_delta", delta: "hidden" });
        // 模型只通过当前回合绑定的函数申请工具执行。
        await assert.rejects(
          () => input.executeTool({
            toolName: "run-shell",
            args: { command: "rm -rf /" },
          }),
        );
        const executed = await input.executeTool({
          toolName: "positions",
          args: {},
        });
        assert.equal((executed.data as { data: { count: number } }).data.count, 0);
        input.emitSignal({ kind: "message_delta", delta: "当前有持仓记录。" });
        input.emitSignal({ kind: "usage_updated", totalTokens: 18, totalCost: 0.5 });
        input.emitSignal({
          kind: "message_snapshot",
          content: "当前有持仓记录。",
          stopReason: "end_turn",
        });
        input.emitSignal({ kind: "turn_settled" });
      };
      const failureExecutor: AgentTurnExecutor = async (input) => {
        input.emitSignal({ kind: "message_delta", delta: "部分" });
        input.emitSignal({ kind: "message_failed", outcome: "failed" });
        input.emitSignal({ kind: "turn_settled" });
      };
      // 测试通过可变持有者在回合间切换执行器，orchestrator 本身保持不变。
      let activeExecutor: AgentTurnExecutor = successExecutor;
      const orchestrator = new AgentTurnOrchestrator({
        database,
        writeCoordinator: new WriteCoordinator(),
        registry: createFinancialToolRegistry(database),
        executor: (input) => activeExecutor(input),
      });

      const opened = await orchestrator.openSession(SESSION_ID);
      assert.equal(opened.status, "idle");

      const run = await orchestrator.startTurn(SESSION_ID, " 分析当前持仓 ");
      assert.equal(observedContent, "分析当前持仓");
      assert.equal(run.snapshot.status, "idle");
      assert.equal(run.snapshot.revision, 3);
      assert.deepEqual(run.events.map((event) => event.type), [
        "message.started",
        "reasoning.delta",
        "tool.completed",
        "tool.started",
        "tool.completed",
        "message.delta",
        "usage.updated",
        "message.completed",
      ]);
      assert.deepEqual(
        run.events.map((event) => event.sequence),
        run.events.map((_, index) => index + 1),
      );

      const resumed = orchestrator.resumeEvents(SESSION_ID, 3);
      assert.deepEqual(resumed, {
        mode: "events",
        events: run.events.slice(3),
      });

      const stored = await database.connection.runAndReadAll(`
        SELECT
          (SELECT last_event_sequence FROM agent_sessions
           WHERE id = CAST($session_id AS UUID)) AS last_event_sequence,
          (SELECT count(*)::INTEGER FROM agent_messages
           WHERE session_id = CAST($session_id AS UUID)) AS message_count,
          (SELECT content FROM agent_messages
           WHERE session_id = CAST($session_id AS UUID)
             AND role = 'assistant' ORDER BY ordinal LIMIT 1) AS assistant_content,
          (SELECT count(*)::INTEGER FROM agent_tool_calls
           WHERE session_id = CAST($session_id AS UUID)
             AND status = 'rejected'
             AND authorization_decision = 'denied') AS rejected_calls,
          (SELECT count(*)::INTEGER FROM agent_tool_calls
           WHERE session_id = CAST($session_id AS UUID)
             AND status = 'completed'
             AND tool_name = 'positions'
             AND duration_ms >= 0) AS completed_calls,
          (SELECT count(*)::INTEGER FROM audit_events
           WHERE event_type = 'agent.tool_rejected') AS audit_rows
      `, { session_id: SESSION_ID });
      assert.deepEqual(stored.getRowObjectsJson()[0], {
        last_event_sequence: "8",
        message_count: 2,
        assistant_content: "当前有持仓记录。",
        rejected_calls: 1,
        completed_calls: 1,
        audit_rows: 1,
      });

      // 第二个回合：供应商失败信号必须落为 failed 助手消息并恢复 idle。
      await assert.rejects(() => orchestrator.startTurn(SESSION_ID, ""));
      activeExecutor = failureExecutor;
      const failureRun = await orchestrator.startTurn(SESSION_ID, "继续分析风险");
      assert.equal(failureRun.snapshot.status, "idle");
      assert.equal(failureRun.snapshot.revision, 5);
      assert.equal(failureRun.events.at(-1)?.type, "message.failed");

      const failedRows = await database.connection.runAndReadAll(`
        SELECT status, error_code
        FROM agent_messages
        WHERE session_id = CAST($session_id AS UUID)
        ORDER BY ordinal
      `, { session_id: SESSION_ID });
      assert.deepEqual(failedRows.getRowObjectsJson().at(-1), {
        status: "failed",
        error_code: "AGENT_TURN_FAILED",
      });

      database.close();
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });

  it("取消 running 回合并经 cancelling 恢复 idle", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-orchestrator-cancel-"));
    const databasePath = join(directory, "ledger.duckdb");
    try {
      await seedLedger(databasePath);
      const database = await openLyraDatabase(databasePath);
      const session = createAgentSession({
        sessionId: CANCEL_SESSION_ID,
        provider: "genkit",
        availability: { provider: "genkit", status: "available" },
        accountScope: { kind: "all" },
        ledgerVersion: 1,
        asOf: "2026-09-01T09:00:00.000Z",
      });
      await persistNewAgentSession(database, {
        session,
        deviceId: DEVICE_ID,
        createdAt: new Date("2026-09-01T09:00:00.000Z"),
      });

      const orchestrator = new AgentTurnOrchestrator({
        database,
        writeCoordinator: new WriteCoordinator(),
        registry: createFinancialToolRegistry(database),
        executor: async (input) => {
            turnStarted();
            await new Promise<void>((resolve) => {
              if (input.abortSignal.aborted) {
                resolve();
                return;
              }
              input.abortSignal.addEventListener("abort", () => resolve(), { once: true });
            });
            input.emitSignal({ kind: "message_failed", outcome: "cancelled" });
            input.emitSignal({ kind: "turn_settled" });
          },
      });
      await orchestrator.openSession(CANCEL_SESSION_ID);
      // 执行器入口作为回合已注册的确定性信号，避免在回合启动落库前取消。
      let signalTurnStarted: () => void = () => undefined;
      const turnStarted = () => signalTurnStarted();
      const turnStartedLatch = new Promise<void>((resolve) => {
        signalTurnStarted = resolve;
      });
      const runPromise = orchestrator.startTurn(CANCEL_SESSION_ID, "长时间分析");
      await turnStartedLatch;
      const cancelling = await orchestrator.cancelTurn(CANCEL_SESSION_ID);
      assert.equal(cancelling.status, "cancelling");
      const run = await runPromise;
      assert.equal(run.snapshot.status, "idle");
      assert.equal(run.events.at(-1)?.type, "message.failed");

      const rows = await database.connection.runAndReadAll(`
        SELECT
          (SELECT status FROM agent_sessions WHERE id = CAST($session_id AS UUID))
            AS session_status,
          (SELECT status FROM agent_messages
           WHERE session_id = CAST($session_id AS UUID)
             AND role = 'assistant') AS message_status,
          (SELECT error_code FROM agent_messages
           WHERE session_id = CAST($session_id AS UUID)
             AND role = 'assistant') AS error_code
      `, { session_id: CANCEL_SESSION_ID });
      // 取消时执行器尚未产出可见内容；空部分回答不伪造助手消息。
      assert.deepEqual(rows.getRowObjectsJson()[0], {
        session_status: "idle",
        message_status: null,
        error_code: null,
      });

      await assert.rejects(
        () => orchestrator.cancelTurn(CANCEL_SESSION_ID),
      );
      database.close();
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });
});
