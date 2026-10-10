import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { ApplicationError } from "../src/application/index.js";
import {
  completeAgentToolCall,
  createAgentSession,
  rejectAgentToolCall,
  startAgentToolCall,
  startAgentTurn,
} from "../src/domain/agent/index.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";
import {
  loadAgentSessionRuntime,
  persistAgentTurnStart,
  persistNewAgentSession,
} from "../src/infrastructure/db/agent-session-repository.js";
import {
  persistAgentToolCallCompletion,
  persistAgentToolCallRejection,
  persistAgentToolCallStart,
} from "../src/infrastructure/db/agent-tool-call-repository.js";

const ACCOUNT_ID = "75000000-0000-4000-8000-000000000001";
const DEVICE_ID = "76000000-0000-4000-8000-000000000001";
const SESSION_ID = "77000000-0000-4000-8000-000000000001";
const USER_MESSAGE_ID = "78000000-0000-4000-8000-000000000001";
const COMPLETED_CALL_ID = "79000000-0000-4000-8000-000000000001";
const FAILED_CALL_ID = "79000000-0000-4000-8000-000000000002";
const REJECTED_CALL_ID = "79000000-0000-4000-8000-000000000003";

describe("Agent tool call write repository", () => {
  it("原子持久化 started、终态与拒绝工具调用，并推进事件序号与审计事件", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-tool-call-write-"));
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
          CAST($device_id AS UUID), 'Android phone', 'public-key', repeat('c', 64),
          'Ed25519', CAST('2026-09-01T08:00:00Z' AS TIMESTAMPTZ),
          CAST('2026-09-01T08:00:00Z' AS TIMESTAMPTZ)
        )
      `, { device_id: DEVICE_ID });
      await database.connection.run(`
        INSERT INTO ledger_versions (reason, source_reference, created_at)
        VALUES ('file_import', 'seed', CAST('2026-09-01T08:30:00Z' AS TIMESTAMPTZ))
      `);

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
      const turn = startAgentTurn({
        session,
        messageId: USER_MESSAGE_ID,
        content: "解释当前持仓",
        startedAt: "2026-09-01T09:01:00.000Z",
        lastEventSequence: 0,
      });
      await persistAgentTurnStart(database, turn);

      const running = await loadAgentSessionRuntime(database, SESSION_ID);
      if (running === null) {
        throw new Error("测试 running 会话未恢复");
      }
      const startedCall = startAgentToolCall({
        session: running.snapshot,
        messageId: USER_MESSAGE_ID,
        callId: COMPLETED_CALL_ID,
        ordinal: 1,
        toolName: "positions",
        argumentsSummary: { account_scope: "all" },
        startedAt: "2026-09-01T09:01:10.000Z",
        lastEventSequence: running.lastEventSequence,
      });
      assert.equal(startedCall.event.sequence, 2);
      await persistAgentToolCallStart(database, startedCall);

      await assert.rejects(
        () => persistAgentToolCallStart(database, startedCall),
        (error: unknown) => {
          assert.ok(error instanceof ApplicationError);
          assert.equal(error.code, "AGENT_SESSION_STATE_CONFLICT");
          return true;
        },
      );

      const afterStart = await database.connection.runAndReadAll(`
        SELECT
          (SELECT status FROM agent_tool_calls WHERE id = CAST($call_id AS UUID))
            AS call_status,
          (SELECT json_extract_string(arguments_summary, '$.account_scope')
           FROM agent_tool_calls WHERE id = CAST($call_id AS UUID)) AS scope_arg,
          (SELECT last_event_sequence FROM agent_sessions
           WHERE id = CAST($session_id AS UUID)) AS last_event_sequence
      `, { call_id: COMPLETED_CALL_ID, session_id: SESSION_ID });
      assert.deepEqual(afterStart.getRowObjectsJson()[0], {
        call_status: "started",
        scope_arg: "all",
        last_event_sequence: "2",
      });

      const completedCall = completeAgentToolCall({
        call: startedCall.call,
        outcome: "completed",
        resultSummary: { position_count: 3 },
        durationMs: 42,
        completedAt: "2026-09-01T09:01:11.000Z",
        lastEventSequence: 2,
      });
      await persistAgentToolCallCompletion(database, completedCall);

      await assert.rejects(
        () => persistAgentToolCallCompletion(database, completedCall),
        (error: unknown) => {
          assert.ok(error instanceof ApplicationError);
          assert.equal(error.code, "AGENT_SESSION_STATE_CONFLICT");
          return true;
        },
      );

      const runningAgain = await loadAgentSessionRuntime(database, SESSION_ID);
      if (runningAgain === null) {
        throw new Error("测试第二次 running 会话未恢复");
      }
      const failedStart = startAgentToolCall({
        session: runningAgain.snapshot,
        messageId: USER_MESSAGE_ID,
        callId: FAILED_CALL_ID,
        ordinal: 2,
        toolName: "expiring",
        argumentsSummary: {},
        startedAt: "2026-09-01T09:01:20.000Z",
        lastEventSequence: runningAgain.lastEventSequence,
      });
      await persistAgentToolCallStart(database, failedStart);
      const failedCall = completeAgentToolCall({
        call: failedStart.call,
        outcome: "failed",
        resultSummary: null,
        durationMs: 15,
        completedAt: "2026-09-01T09:01:21.000Z",
        lastEventSequence: failedStart.event.sequence,
      });
      await persistAgentToolCallCompletion(database, failedCall);

      const terminalRows = await database.connection.runAndReadAll(`
        SELECT id, status, result_summary, duration_ms, error_code
        FROM agent_tool_calls
        WHERE session_id = CAST($session_id AS UUID)
        ORDER BY ordinal
      `, { session_id: SESSION_ID });
      assert.deepEqual(terminalRows.getRowObjectsJson(), [
        {
          id: COMPLETED_CALL_ID,
          status: "completed",
          result_summary: '{"position_count":3}',
          duration_ms: "42",
          error_code: null,
        },
        {
          id: FAILED_CALL_ID,
          status: "failed",
          result_summary: null,
          duration_ms: "15",
          error_code: "AGENT_TOOL_EXECUTION_FAILED",
        },
      ]);

      const rejection = rejectAgentToolCall({
        session: runningAgain.snapshot,
        messageId: USER_MESSAGE_ID,
        callId: REJECTED_CALL_ID,
        ordinal: 3,
        requestedToolName: "run-shell",
        reason: "unknown_tool",
        rejectedAt: "2026-09-01T09:01:30.000Z",
        lastEventSequence: failedCall.event.sequence,
        requestId: "req-tool-reject",
      });
      await persistAgentToolCallRejection(database, rejection);

      const stored = await database.connection.runAndReadAll(`
        SELECT
          (SELECT last_event_sequence FROM agent_sessions
           WHERE id = CAST($session_id AS UUID)) AS last_event_sequence,
          (SELECT count(*)::INTEGER FROM agent_tool_calls
           WHERE id = CAST($call_id AS UUID)
             AND authorization_decision = 'denied'
             AND status = 'rejected'
             AND error_code = 'AGENT_TOOL_FORBIDDEN') AS rejected_rows,
          (SELECT count(*)::INTEGER FROM audit_events
           WHERE target_id = $call_id
             AND event_type = 'agent.tool_rejected'
             AND outcome = 'denied'
             AND error_code = 'AGENT_TOOL_FORBIDDEN'
             AND json_extract_string(metadata_json, '$.policy_reason') = 'unknown_tool'
             AND json_extract_string(metadata_json, '$.tool_name') = 'run-shell')
            AS audit_rows
      `, { session_id: SESSION_ID, call_id: REJECTED_CALL_ID });
      assert.deepEqual(stored.getRowObjectsJson()[0], {
        last_event_sequence: "6",
        rejected_rows: 1,
        audit_rows: 1,
      });
    } finally {
      database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
});
