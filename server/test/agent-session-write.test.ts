import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { ApplicationError } from "../src/application/index.js";
import {
  completeAgentTurn,
  createAgentSession,
  failAgentTurn,
  startAgentTurn,
} from "../src/domain/agent/index.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";
import {
  loadAgentSessionRuntime,
  persistAgentTurnCompletion,
  persistAgentTurnFailure,
  persistAgentTurnStart,
  persistNewAgentSession,
} from "../src/infrastructure/db/agent-session-repository.js";

const ACCOUNT_ID = "71000000-0000-4000-8000-000000000001";
const DEVICE_ID = "72000000-0000-4000-8000-000000000001";
const SESSION_ID = "73000000-0000-4000-8000-000000000001";
const MESSAGE_ID = "74000000-0000-4000-8000-000000000001";
const ASSISTANT_ID = "74000000-0000-4000-8000-000000000002";
const SECOND_USER_ID = "74000000-0000-4000-8000-000000000003";
const FAILED_ASSISTANT_ID = "74000000-0000-4000-8000-000000000004";

describe("Agent session write repository", () => {
  it("原子持久化会话、回合启动与成功/失败收口，并让重复计划回滚", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-agent-write-"));
    const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
    try {
      await database.connection.run(`
        INSERT INTO accounts (
          id, display_name, legacy_account_key, masked_suffix, created_at, updated_at
        ) VALUES (
          CAST($account_id AS UUID), 'Primary', 'SCHWAB-PRIMARY', '1001',
          CAST('2026-08-31T08:00:00Z' AS TIMESTAMPTZ),
          CAST('2026-08-31T08:00:00Z' AS TIMESTAMPTZ)
        )
      `, { account_id: ACCOUNT_ID });
      await database.connection.run(`
        INSERT INTO paired_devices (
          id, display_name, public_key_pem, public_key_fingerprint, key_algorithm,
          paired_at, updated_at
        ) VALUES (
          CAST($device_id AS UUID), 'Android phone', 'public-key', repeat('b', 64),
          'Ed25519', CAST('2026-08-31T08:00:00Z' AS TIMESTAMPTZ),
          CAST('2026-08-31T08:00:00Z' AS TIMESTAMPTZ)
        )
      `, { device_id: DEVICE_ID });
      await database.connection.run(`
        INSERT INTO ledger_versions (reason, source_reference, created_at)
        VALUES (
          'file_import', 'seed', CAST('2026-08-31T08:30:00Z' AS TIMESTAMPTZ)
        )
      `);

      const session = createAgentSession({
        sessionId: SESSION_ID,
        provider: "genkit",
        availability: { provider: "genkit", status: "available" },
        accountScope: { kind: "account", account: ACCOUNT_ID },
        ledgerVersion: 1,
        asOf: "2026-08-31T09:00:00.000Z",
      });
      await persistNewAgentSession(database, {
        session,
        deviceId: DEVICE_ID,
        createdAt: new Date("2026-08-31T09:00:00.000Z"),
      });

      const created = await loadAgentSessionRuntime(database, SESSION_ID);
      assert.deepEqual(created, { deviceId: DEVICE_ID, snapshot: session, lastEventSequence: 0 });
      if (created === null) {
        throw new Error("测试 Agent 会话未持久化");
      }
      const plan = startAgentTurn({
        session: created.snapshot,
        messageId: MESSAGE_ID,
        content: " 分析当前持仓 ",
        startedAt: "2026-08-31T09:01:00.000Z",
        lastEventSequence: created.lastEventSequence,
      });
      await persistAgentTurnStart(database, plan);

      const running = await loadAgentSessionRuntime(database, SESSION_ID);
      assert.equal(running?.snapshot.status, "running");
      assert.equal(running?.snapshot.revision, 2);
      assert.equal(running?.lastEventSequence, 1);
      assert.deepEqual(running?.snapshot.messages, [{
        id: MESSAGE_ID,
        role: "user",
        content: "分析当前持仓",
        createdAt: "2026-08-31T09:01:00.000Z",
      }]);

      await assert.rejects(
        () => persistAgentTurnStart(database, plan),
        (error: unknown) => {
          assert.ok(error instanceof ApplicationError);
          assert.equal(error.code, "AGENT_SESSION_STATE_CONFLICT");
          return true;
        },
      );
      const stored = await database.connection.runAndReadAll(`
        SELECT
          (SELECT count(*)::INTEGER FROM agent_messages) AS message_count,
          (SELECT status FROM agent_messages WHERE id = CAST($message_id AS UUID)) AS status,
          (SELECT provider FROM agent_messages WHERE id = CAST($message_id AS UUID)) AS provider,
          (SELECT last_event_sequence FROM agent_sessions
           WHERE id = CAST($session_id AS UUID)) AS last_event_sequence
      `, { message_id: MESSAGE_ID, session_id: SESSION_ID });
      assert.deepEqual(stored.getRowObjectsJson()[0], {
        message_count: 1,
        status: "completed",
        provider: null,
        last_event_sequence: "1",
      });

      if (running === null) {
        throw new Error("测试 Agent running 会话未恢复");
      }
      const completion = completeAgentTurn({
        session: running.snapshot,
        messageId: ASSISTANT_ID,
        content: " 当前有一个未平期权持仓。 ",
        completedAt: "2026-08-31T09:02:00.000Z",
        lastEventSequence: running.lastEventSequence,
      });
      await persistAgentTurnCompletion(database, completion);
      const completed = await loadAgentSessionRuntime(database, SESSION_ID);
      assert.equal(completed?.snapshot.status, "idle");
      assert.equal(completed?.snapshot.revision, 3);
      assert.equal(completed?.lastEventSequence, 2);
      assert.equal(completed?.snapshot.messages[1]?.status, "completed");

      if (completed === null) {
        throw new Error("测试 Agent 完成会话未恢复");
      }
      const secondStart = startAgentTurn({
        session: completed.snapshot,
        messageId: SECOND_USER_ID,
        content: "继续分析风险",
        startedAt: "2026-08-31T09:03:00.000Z",
        lastEventSequence: completed.lastEventSequence,
      });
      await persistAgentTurnStart(database, secondStart);
      const secondRunning = await loadAgentSessionRuntime(database, SESSION_ID);
      if (secondRunning === null) {
        throw new Error("测试 Agent 第二回合未恢复");
      }
      const failure = failAgentTurn({
        session: secondRunning.snapshot,
        messageId: FAILED_ASSISTANT_ID,
        outcome: "failed",
        partialContent: "已读取持仓，但分析未完成。",
        finishedAt: "2026-08-31T09:04:00.000Z",
        lastEventSequence: secondRunning.lastEventSequence,
      });
      await persistAgentTurnFailure(database, failure);

      const failed = await loadAgentSessionRuntime(database, SESSION_ID);
      assert.equal(failed?.snapshot.status, "idle");
      assert.equal(failed?.snapshot.revision, 5);
      assert.equal(failed?.lastEventSequence, 4);
      assert.deepEqual(failed?.snapshot.messages.at(-1), {
        id: FAILED_ASSISTANT_ID,
        role: "assistant",
        content: "已读取持仓，但分析未完成。",
        createdAt: "2026-08-31T09:04:00.000Z",
        status: "failed",
        errorCode: "AGENT_TURN_FAILED",
      });
      const terminalRows = await database.connection.runAndReadAll(`
        SELECT role, provider, status, error_code
        FROM agent_messages
        WHERE session_id = CAST($session_id AS UUID)
        ORDER BY ordinal
      `, { session_id: SESSION_ID });
      assert.deepEqual(terminalRows.getRowObjectsJson(), [
        { role: "user", provider: null, status: "completed", error_code: null },
        { role: "assistant", provider: "genkit", status: "completed", error_code: null },
        { role: "user", provider: null, status: "completed", error_code: null },
        {
          role: "assistant",
          provider: "genkit",
          status: "failed",
          error_code: "AGENT_TURN_FAILED",
        },
      ]);
    } finally {
      database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
});
