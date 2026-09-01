import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import type { AgentSessionSnapshot } from "../src/domain/agent/index.js";
import {
  AgentMcpCredentialStore,
  authenticateAgentMcpCredential,
  issueAgentMcpCredential,
} from "../src/infrastructure/agents/index.js";
import { loadAgentSession } from "../src/infrastructure/db/agent-session-repository.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";

const ACCOUNT_ID = "10000000-0000-4000-8000-000000000001";
const DEVICE_ID = "20000000-0000-4000-8000-000000000001";
const SESSION_ID = "30000000-0000-4000-8000-000000000001";
const USER_MESSAGE_ID = "40000000-0000-4000-8000-000000000001";
const ASSISTANT_MESSAGE_ID = "40000000-0000-4000-8000-000000000002";

/** 为仓储测试写入满足 v1/v3 外键的最小账户和设备。 */
async function seedAgentSession(databasePath: string): Promise<void> {
  const database = await openLyraDatabase(databasePath);
  try {
    await database.connection.run(`
      INSERT INTO accounts (
        id, display_name, legacy_account_key, masked_suffix, base_currency,
        created_at, updated_at
      ) VALUES (
        CAST($account_id AS UUID), 'Primary', 'SCHWAB001', '0001', 'USD',
        CAST('2026-08-31T09:00:00Z' AS TIMESTAMPTZ),
        CAST('2026-08-31T09:00:00Z' AS TIMESTAMPTZ)
      )
    `, { account_id: ACCOUNT_ID });
    await database.connection.run(`
      INSERT INTO paired_devices (
        id, display_name, public_key_pem, public_key_fingerprint, key_algorithm,
        paired_at, updated_at
      ) VALUES (
        CAST($device_id AS UUID), 'Test phone', 'test-public-key', repeat('a', 64),
        'Ed25519', CAST('2026-08-31T09:00:00Z' AS TIMESTAMPTZ),
        CAST('2026-08-31T09:00:00Z' AS TIMESTAMPTZ)
      )
    `, { device_id: DEVICE_ID });
    await database.connection.run(`
      INSERT INTO agent_session_identities (id)
      VALUES (CAST($session_id AS UUID))
    `, { session_id: SESSION_ID });
    await database.connection.run(`
      INSERT INTO agent_sessions (
        id, device_id, provider, status, account_scope_kind, account_id,
        ledger_version, as_of, revision, last_event_sequence, summary,
        created_at, updated_at
      ) VALUES (
        CAST($session_id AS UUID), CAST($device_id AS UUID), 'claude', 'running',
        'account', CAST($account_id AS UUID), 31,
        CAST('2026-08-31T10:00:00Z' AS TIMESTAMPTZ), 4, 2, '已完成摘要',
        CAST('2026-08-31T10:00:00Z' AS TIMESTAMPTZ),
        CAST('2026-08-31T10:01:00Z' AS TIMESTAMPTZ)
      )
    `, { session_id: SESSION_ID, device_id: DEVICE_ID, account_id: ACCOUNT_ID });
    await database.connection.run(`
      INSERT INTO agent_messages (
        id, session_id, ordinal, role, provider, status, content, error_code,
        created_at, completed_at
      ) VALUES
        (
          CAST($user_id AS UUID), CAST($session_id AS UUID), 1, 'user', NULL,
          'completed', '分析本月结果', NULL,
          CAST('2026-08-31T10:00:10Z' AS TIMESTAMPTZ),
          CAST('2026-08-31T10:00:10Z' AS TIMESTAMPTZ)
        ),
        (
          CAST($assistant_id AS UUID), CAST($session_id AS UUID), 2, 'assistant',
          'claude', 'completed', '本月结果如下', NULL,
          CAST('2026-08-31T10:00:20Z' AS TIMESTAMPTZ),
          CAST('2026-08-31T10:00:20Z' AS TIMESTAMPTZ)
        )
    `, {
      user_id: USER_MESSAGE_ID,
      assistant_id: ASSISTANT_MESSAGE_ID,
      session_id: SESSION_ID,
    });
  } finally {
    database.close();
  }
}

describe("Agent production runtime stores", () => {
  it("从真实 DuckDB 按 ordinal 重建规范会话与 Lyra 账户范围", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-agent-session-"));
    const databasePath = join(directory, "ledger.duckdb");
    try {
      await seedAgentSession(databasePath);
      const database = await openLyraDatabase(databasePath);
      try {
        const session = await loadAgentSession(database, SESSION_ID);
        assert.deepEqual(session, {
          id: SESSION_ID,
          provider: "claude",
          status: "running",
          accountScope: { kind: "account", account: ACCOUNT_ID },
          ledgerVersion: 31,
          asOf: "2026-08-31T10:00:00.000Z",
          revision: 4,
          summary: "已完成摘要",
          messages: [
            {
              id: USER_MESSAGE_ID,
              role: "user",
              content: "分析本月结果",
              createdAt: "2026-08-31T10:00:10.000Z",
            },
            {
              id: ASSISTANT_MESSAGE_ID,
              role: "assistant",
              content: "本月结果如下",
              createdAt: "2026-08-31T10:00:20.000Z",
              status: "completed",
              errorCode: null,
            },
          ],
        });
        assert.equal(
          await loadAgentSession(
            database,
            "30000000-0000-4000-8000-000000000099",
          ),
          null,
        );
      } finally {
        database.close();
      }
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });

  it("只保存 MCP 哈希记录，并回存使用时间后按会话撤销", () => {
    const session: AgentSessionSnapshot = {
      id: SESSION_ID,
      provider: "claude",
      status: "running",
      accountScope: { kind: "account", account: ACCOUNT_ID },
      ledgerVersion: 31,
      asOf: "2026-08-31T10:00:00.000Z",
      revision: 4,
      messages: [],
    };
    const issued = issueAgentMcpCredential({
      session,
      now: new Date("2026-08-31T10:00:00.000Z"),
      ttlMilliseconds: 60_000,
    });
    const store = new AgentMcpCredentialStore();
    store.add(issued.record);

    const loaded = store.findByTokenHash(issued.record.tokenHash);
    assert.notEqual(loaded, null);
    if (loaded === null) {
      throw new Error("测试 MCP 凭据未写入存储");
    }
    const authenticated = authenticateAgentMcpCredential({
      session,
      record: loaded,
      token: issued.token,
      now: new Date("2026-08-31T10:00:15.000Z"),
    });
    store.update(authenticated.record);
    assert.equal(
      store.findByTokenHash(issued.record.tokenHash)?.lastUsedAt,
      "2026-08-31T10:00:15.000Z",
    );

    assert.equal(
      store.revokeSession(SESSION_ID, new Date("2026-08-31T10:00:20.000Z")),
      1,
    );
    assert.equal(store.findByTokenHash(issued.record.tokenHash)?.status, "revoked");
  });
});
