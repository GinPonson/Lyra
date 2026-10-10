import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { describe, it } from "node:test";
import { DuckDBInstance } from "@duckdb/node-api";

import { createAgentSession } from "../src/domain/agent/index.js";
import {
  loadAgentSession,
  persistNewAgentSession,
  recoverInterruptedAgentSessions,
} from "../src/infrastructure/db/agent-session-repository.js";
import { loadAgentToolCalls } from "../src/infrastructure/db/agent-tool-call-repository.js";
import { LyraDatabase } from "../src/infrastructure/db/database.js";

const ACCOUNT_ID = "f1000000-0000-4000-8000-000000000001";
const DEVICE_ID = "f2000000-0000-4000-8000-000000000001";
const WHEN = "2026-10-10T10:00:00.000Z";
const PRIOR_MIGRATIONS = [
  "schema.sql", "gmail-schema.sql", "auth-schema.sql", "audit-schema.sql",
  "agent-schema.sql", "access-token-schema.sql", "import-preview-schema.sql",
  "reconciliation-reminder-schema.sql", "transaction-source-view-schema.sql",
  "idempotency-schema.sql", "agent-session-identity-schema.sql",
];

async function sql(name: string): Promise<string> {
  return readFile(new URL(`../src/infrastructure/db/${name}`, import.meta.url), "utf8");
}

/** Build the actual previous schema, rather than weakening new constraints for a fixture. */
async function oldDatabase(): Promise<LyraDatabase> {
  const instance = await DuckDBInstance.create(":memory:");
  const database = new LyraDatabase(instance, await instance.connect());
  for (const migration of PRIOR_MIGRATIONS) await database.connection.run(await sql(migration));
  await database.connection.run(`
    INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
    VALUES (CAST($account_id AS UUID), 'Unchanged account', 'MIGRATION-TEST', '0001')
  `, { account_id: ACCOUNT_ID });
  await database.connection.run(`
    INSERT INTO paired_devices (
      id, display_name, public_key_pem, public_key_fingerprint, key_algorithm, paired_at, updated_at
    ) VALUES (CAST($device_id AS UUID), 'Phone', 'public-key', repeat('b', 64), 'Ed25519',
      CAST($now AS TIMESTAMPTZ), CAST($now AS TIMESTAMPTZ))
  `, { device_id: DEVICE_ID, now: WHEN });
  await database.connection.run("INSERT INTO ledger_versions (reason, source_reference) VALUES ('file_import', 'preserve-me')");
  await database.connection.run(`
    INSERT INTO transactions (
      account_id, record_date, transaction_date, action, asset_type, amount,
      source_type, source_reference, source_hash, sequence, occurrence,
      business_hash, transaction_hash, parser_version, ledger_version
    ) VALUES (CAST($account_id AS UUID), DATE '2026-10-10', DATE '2026-10-10',
      'MoneyLink Transfer', 'cash', 1000.25, 'csv', 'fixture.csv', repeat('c', 64),
      1, 1, repeat('d', 64), repeat('e', 64), 'fixture', 1)
  `, { account_id: ACCOUNT_ID });
  return database;
}

async function seedConversation(database: LyraDatabase, provider: string, status: "running" | "cancelling") {
  const sessionId = randomUUID();
  const userId = randomUUID();
  const assistantId = randomUUID();
  await database.connection.run("INSERT INTO agent_session_identities VALUES (CAST($id AS UUID))", { id: sessionId });
  await database.connection.run(`
    INSERT INTO agent_sessions (
      id, device_id, provider, status, account_scope_kind, account_id,
      ledger_version, as_of, revision, last_event_sequence, summary, created_at, updated_at
    ) VALUES (CAST($id AS UUID), CAST($device AS UUID), $provider, $status, 'account',
      CAST($account AS UUID), 1, CAST($now AS TIMESTAMPTZ), 4, 7, 'durable summary',
      CAST($now AS TIMESTAMPTZ), CAST($now AS TIMESTAMPTZ))
  `, { id: sessionId, device: DEVICE_ID, provider, status, account: ACCOUNT_ID, now: WHEN });
  await database.connection.run(`
    INSERT INTO agent_messages (
      id, session_id, ordinal, role, provider, status, content, error_code, created_at, completed_at
    ) VALUES
      (CAST($user_id AS UUID), CAST($session_id AS UUID), 1, 'user', NULL, 'completed',
        '保留我的持仓问题', NULL, CAST($now AS TIMESTAMPTZ), CAST($now AS TIMESTAMPTZ)),
      (CAST($assistant_id AS UUID), CAST($session_id AS UUID), 2, 'assistant', $provider, 'streaming',
        '保留已经保存的部分回答', NULL, CAST($now AS TIMESTAMPTZ), NULL)
  `, { user_id: userId, assistant_id: assistantId, session_id: sessionId, provider, now: WHEN });
  await database.connection.run(`
    INSERT INTO agent_tool_calls (
      id, session_id, message_id, ordinal, tool_name, authorization_decision, status,
      arguments_summary, result_summary, ledger_version, duration_ms, error_code, started_at, completed_at
    ) VALUES
      (uuid(), CAST($session AS UUID), CAST($message AS UUID), 1, 'positions', 'allowed', 'completed',
        '{}', '{"count":1}', 1, 5, NULL, CAST($now AS TIMESTAMPTZ), CAST($now AS TIMESTAMPTZ)),
      (uuid(), CAST($session AS UUID), CAST($message AS UUID), 2, 'stress', 'allowed', 'started',
        '{}', NULL, 1, NULL, NULL, CAST($now AS TIMESTAMPTZ), NULL)
  `, { session: sessionId, message: userId, now: WHEN });
  return { sessionId, userId, assistantId };
}

async function financialRows(database: LyraDatabase) {
  return (await database.connection.runAndReadAll("SELECT * FROM transactions ORDER BY transaction_hash")).getRowObjectsJson();
}

describe("Genkit migration and recovery", () => {
  it("upgrades populated v11, archives legacy sessions explicitly and preserves financial facts, visible messages and tool audit", async () => {
    const database = await oldDatabase();
    try {
      const legacy = await seedConversation(database, "claude", "running");
      const financialBefore = await financialRows(database);
      await database.connection.run("BEGIN TRANSACTION");
      await database.connection.run(await sql("agent-genkit-schema.sql"));
      await database.connection.run("COMMIT");

      assert.deepEqual(await financialRows(database), financialBefore);
      const row = (await database.connection.runAndReadAll(`
        SELECT provider, original_provider, status, revision, last_event_sequence,
          CAST(account_id AS VARCHAR) AS account_id, ledger_version
        FROM agent_sessions WHERE id = CAST($id AS UUID)
      `, { id: legacy.sessionId })).getRowObjectsJson()[0];
      assert.deepEqual(row, {
        provider: "genkit", original_provider: "claude", status: "closed",
        revision: 5, last_event_sequence: "8", account_id: ACCOUNT_ID, ledger_version: "1",
      });
      const session = await loadAgentSession(database, legacy.sessionId);
      assert.equal(session?.messages.length, 3);
      assert.equal(session?.messages[0]?.id, legacy.userId);
      assert.equal(session?.messages[0]?.content, "保留我的持仓问题");
      assert.equal(session?.messages[1]?.id, legacy.assistantId);
      assert.equal(session?.messages[1]?.content, "保留已经保存的部分回答");
      assert.equal(session?.messages[1]?.status, "failed");
      assert.match(session?.messages.at(-1)?.content ?? "", /claude.*Genkit.*新建/);
      const calls = await loadAgentToolCalls(database, legacy.sessionId);
      assert.deepEqual(calls?.map((call) => [call.toolName, call.status]), [["positions", "completed"], ["stress", "cancelled"]]);
      assert.deepEqual(calls?.[0]?.resultSummary, { count: 1 });

      const fresh = createAgentSession({
        sessionId: randomUUID(), provider: "genkit", availability: { provider: "genkit", status: "available" },
        accountScope: { kind: "account", account: ACCOUNT_ID }, ledgerVersion: 1, asOf: WHEN,
      });
      await persistNewAgentSession(database, { session: fresh, deviceId: DEVICE_ID, createdAt: new Date(WHEN) });
      assert.equal((await loadAgentSession(database, fresh.id))?.provider, "genkit");
      assert.equal((await loadAgentSession(database, fresh.id))?.status, "idle");
      assert.equal((await database.connection.runAndReadAll("SELECT count(*)::INTEGER AS count FROM schema_migrations WHERE version = 12")).getRowObjectsJson()[0]?.count, 1);
    } finally {
      database.close();
    }
  });

  it("recovers running/cancelling Genkit turns, preserves partial content and completed tools, and is idempotent", async () => {
    const database = await oldDatabase();
    try {
      await database.connection.run(await sql("agent-genkit-schema.sql"));
      const running = await seedConversation(database, "genkit", "running");
      const cancelling = await seedConversation(database, "genkit", "cancelling");
      const before = await financialRows(database);
      assert.equal(await recoverInterruptedAgentSessions(database, new Date("2026-10-10T11:00:00Z")), 2);
      for (const id of [running.sessionId, cancelling.sessionId]) {
        const session = await loadAgentSession(database, id);
        assert.equal(session?.status, "idle");
        assert.equal(session?.revision, 5);
        assert.equal(session?.messages.length, 3);
        assert.equal(session?.messages[1]?.content, "保留已经保存的部分回答");
        assert.equal(session?.messages[1]?.status, "failed");
        assert.match(session?.messages.at(-1)?.content ?? "", /服务重启中断/);
        assert.deepEqual((await loadAgentToolCalls(database, id))?.map((call) => call.status), ["completed", "cancelled"]);
      }
      assert.equal(await recoverInterruptedAgentSessions(database, new Date("2026-10-10T12:00:00Z")), 0);
      assert.equal((await loadAgentSession(database, running.sessionId))?.messages.length, 3);
      assert.deepEqual(await financialRows(database), before);
    } finally {
      database.close();
    }
  });
});
