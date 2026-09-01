import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { LATEST_PROTOCOL_VERSION } from "@modelcontextprotocol/sdk/types.js";

import { assembleAgentMcpRuntime } from "../src/application/index.js";
import { createAgentSession } from "../src/domain/agent/index.js";
import {
  AgentMcpCredentialStore,
  issueAgentMcpCredential,
} from "../src/infrastructure/agents/index.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";
import { buildHttpServer, createServerProtocolMetadata } from "../src/transport/index.js";

const ACCOUNT_ID = "85000000-0000-4000-8000-000000000001";
const DEVICE_ID = "86000000-0000-4000-8000-000000000001";
const SESSION_ID = "87000000-0000-4000-8000-000000000001";

describe("Agent MCP production assembly", () => {
  it("从进程内凭据与真实会话恢复授权，并在撤销后拒绝", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-mcp-assembly-"));
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
          CAST($device_id AS UUID), 'Android phone', 'public-key', repeat('f', 64),
          'Ed25519', CAST('2026-09-01T08:00:00Z' AS TIMESTAMPTZ),
          CAST('2026-09-01T08:00:00Z' AS TIMESTAMPTZ)
        )
      `, { device_id: DEVICE_ID });
      await database.connection.run(`
        INSERT INTO ledger_versions (reason, source_reference, created_at)
        VALUES ('file_import', 'seed', CAST('2026-09-01T08:30:00Z' AS TIMESTAMPTZ))
      `);
      await database.connection.run(`
        INSERT INTO agent_session_identities (id) VALUES (CAST($session_id AS UUID))
      `, { session_id: SESSION_ID });
      await database.connection.run(`
        INSERT INTO agent_sessions (
          id, device_id, provider, status, account_scope_kind, account_id,
          ledger_version, as_of, revision, last_event_sequence, summary,
          created_at, updated_at
        ) VALUES (
          CAST($session_id AS UUID), CAST($device_id AS UUID), 'claude', 'running',
          'account', CAST($account_id AS UUID), 1,
          CAST('2026-09-01T09:00:00Z' AS TIMESTAMPTZ), 2, 1, NULL,
          CAST('2026-09-01T09:00:00Z' AS TIMESTAMPTZ),
          CAST('2026-09-01T09:01:00Z' AS TIMESTAMPTZ)
        )
      `, { session_id: SESSION_ID, device_id: DEVICE_ID, account_id: ACCOUNT_ID });

      const runningSnapshot = createAgentSession({
        sessionId: SESSION_ID,
        provider: "claude",
        availability: { provider: "claude", status: "available" },
        accountScope: { kind: "account", account: ACCOUNT_ID },
        ledgerVersion: 1,
        asOf: "2026-09-01T09:00:00.000Z",
      });
      // 编排器签发的凭据绑定 running revision；数据库快照必须与之逐字段一致。
      // 凭据按真实时钟签发，transport 的 now 同样取真实时钟。
      const issued = issueAgentMcpCredential({
        session: { ...runningSnapshot, status: "running", revision: 2 },
        now: new Date(),
        ttlMilliseconds: 60_000,
      });
      const mcpCredentials = new AgentMcpCredentialStore();
      mcpCredentials.add(issued.record);
      const assembly = assembleAgentMcpRuntime({ database, mcpCredentials });
      const app = buildHttpServer({
        protocolMetadata: createServerProtocolMetadata("0.1.0", 11),
        getHealthSnapshot: async () => {
          throw new Error("测试未使用健康端点");
        },
        mcp: {
          registry: assembly.registry,
          loadAuthorization: assembly.loadAuthorization,
          persistCredentialRecord: assembly.persistCredentialRecord,
        },
      });

      const headers = (token: string): Record<string, string> => ({
        accept: "application/json, text/event-stream",
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        "mcp-protocol-version": LATEST_PROTOCOL_VERSION,
      });

      const unknown = await app.inject({
        method: "POST",
        url: "/mcp",
        headers: {
          accept: "application/json, text/event-stream",
          "content-type": "application/json",
        },
        payload: { jsonrpc: "2.0", id: 1, method: "tools/list", params: {} },
      });
      assert.equal(unknown.statusCode, 401);

      const listed = await app.inject({
        method: "POST",
        url: "/mcp",
        headers: headers(issued.token),
        payload: { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} },
      });
      assert.equal(listed.statusCode, 200);
      const toolNames = listed.json().result.tools
        .map((tool: { name: string }) => tool.name);
      assert.equal(toolNames.length, 17);
      assert.ok(toolNames.includes("accounts"));

      const called = await app.inject({
        method: "POST",
        url: "/mcp",
        headers: headers(issued.token),
        payload: {
          jsonrpc: "2.0",
          id: 3,
          method: "tools/call",
          params: { name: "accounts", arguments: {} },
        },
      });
      assert.equal(called.statusCode, 200);
      const structured = called.json().result.structuredContent;
      assert.equal(structured.kind, "lyra_financial_data");
      assert.deepEqual(structured.provenance.accountScope, {
        kind: "account",
        account: ACCOUNT_ID,
      });
      assert.equal(structured.data.count, 1);
      assert.equal(structured.data.rows[0].id, ACCOUNT_ID);
      assert.equal(
        typeof mcpCredentials.findByTokenHash(issued.record.tokenHash)?.lastUsedAt,
        "string",
      );

      assert.equal(mcpCredentials.revokeSession(SESSION_ID, new Date()), 1);
      const revoked = await app.inject({
        method: "POST",
        url: "/mcp",
        headers: headers(issued.token),
        payload: { jsonrpc: "2.0", id: 4, method: "tools/list", params: {} },
      });
      assert.equal(revoked.statusCode, 401);

      await app.close();
    } finally {
      database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
});
