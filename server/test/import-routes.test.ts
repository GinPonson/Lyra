import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import {
  ImportService,
  WriteCoordinator,
  createDeviceAuthGate,
} from "../src/application/index.js";
import { issueDeviceAccessToken } from "../src/infrastructure/security/device-auth.js";
import { loadPairedDeviceAuthRecord } from "../src/infrastructure/db/device-access-token-repository.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";
import { registerImportRoutes } from "../src/transport/import-routes.js";
import { buildHttpServer, createServerProtocolMetadata } from "../src/transport/index.js";

const DEVICE_ID = "9e000000-0000-4000-8000-000000000001";
const ACCOUNT_ID = "9f000000-0000-4000-8000-000000000001";

const FILE_NAME = "Individual_TST001_Transactions_20260902-080000.csv";
const CSV_CONTENT = Buffer.from([
  "\uFEFF\"Date\",\"Action\",\"Symbol\",\"Description\",\"Quantity\",\"Price\",\"Fees & Comm\",\"Amount\"",
  "\"08/30/2026\",\"Buy\",\"LYRA\",\"LYRA, INC\",\"2\",\"$10.00\",\"$0.01\",\"-$20.01\"",
  "\"08/31/2026\",\"Qualified Dividend\",\"LYRA\",\"LYRA, INC\",\"\",\"\",\"\",\"$1.25\"",
  "",
].join("\r\n"), "utf8");

describe("Import REST routes", () => {
  it("完成上传预览、状态读取、确认提交与取消全流程", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-import-routes-"));
    const stagingDirectory = join(directory, "staging");
    await mkdir(stagingDirectory, { recursive: true });
    const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
    try {
      await database.connection.run(`
        INSERT INTO accounts (
          id, display_name, legacy_account_key, masked_suffix, created_at, updated_at
        ) VALUES (
          CAST($account_id AS UUID), 'Primary', 'TST001', '0001',
          CAST('2026-09-02T08:00:00Z' AS TIMESTAMPTZ),
          CAST('2026-09-02T08:00:00Z' AS TIMESTAMPTZ)
        )
      `, { account_id: ACCOUNT_ID });
      await database.connection.run(`
        INSERT INTO paired_devices (
          id, display_name, public_key_pem, public_key_fingerprint, key_algorithm,
          paired_at, updated_at
        ) VALUES (
          CAST($device_id AS UUID), 'Android phone', 'public-key', repeat('e', 64),
          'Ed25519', CAST('2026-09-02T08:00:00Z' AS TIMESTAMPTZ),
          CAST('2026-09-02T08:00:00Z' AS TIMESTAMPTZ)
        )
      `, { device_id: DEVICE_ID });
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

      const app = buildHttpServer({
        protocolMetadata: createServerProtocolMetadata("0.1.0", 11),
        getHealthSnapshot: async () => {
          throw new Error("测试未使用健康端点");
        },
      });
      registerImportRoutes(app, {
        importService: new ImportService({
          database,
          writeCoordinator: new WriteCoordinator(),
          stagingDirectory,
        }),
        authenticateDevice: createDeviceAuthGate(database),
      });

      const headers = {
        authorization: `Bearer ${issued.token}`,
        "x-lyra-protocol-version": "1",
      };
      const noAuth = await app.inject({
        method: "POST",
        url: "/v1/imports/preview",
        headers: {
          authorization: "Bearer missing",
          "x-lyra-protocol-version": "1",
          "content-type": "application/octet-stream",
          "x-lyra-file-name": FILE_NAME,
        },
        payload: CSV_CONTENT,
      });
      assert.equal(noAuth.statusCode, 401);

      const preview = await app.inject({
        method: "POST",
        url: "/v1/imports/preview",
        headers: { ...headers, "content-type": "application/octet-stream", "x-lyra-file-name": FILE_NAME },
        payload: CSV_CONTENT,
      });
      assert.equal(preview.statusCode, 201);
      const previewBody = preview.json();
      assert.equal(previewBody.preview.totalRows, 2);
      assert.equal(previewBody.preview.insertableRows, 2);
      assert.equal(previewBody.preview.accountId, ACCOUNT_ID);
      assert.equal(typeof previewBody.confirmToken, "string");

      const job = await app.inject({
        method: "GET",
        url: `/v1/imports/${previewBody.importId}`,
        headers,
      });
      assert.equal(job.statusCode, 200);
      assert.equal(job.json().job.status, "preview_ready");

      const badToken = await app.inject({
        method: "POST",
        url: `/v1/imports/${previewBody.importId}/commit`,
        headers,
        payload: { confirmToken: "wrong-token" },
      });
      assert.equal(badToken.statusCode, 409);
      assert.equal(badToken.json().code, "IMPORT_PREVIEW_STALE");

      const commit = await app.inject({
        method: "POST",
        url: `/v1/imports/${previewBody.importId}/commit`,
        headers,
        payload: { confirmToken: previewBody.confirmToken },
      });
      assert.equal(commit.statusCode, 200);
      assert.deepEqual(commit.json(), {
        changed: true,
        ledgerVersion: 1,
        insertedRows: 2,
        skippedRows: 0,
      });

      const committedJob = await app.inject({
        method: "GET",
        url: `/v1/imports/${previewBody.importId}`,
        headers,
      });
      assert.equal(committedJob.json().job.status, "committed");
      assert.equal(committedJob.json().job.committedLedgerVersion, 1);

      const replay = await app.inject({
        method: "POST",
        url: `/v1/imports/${previewBody.importId}/commit`,
        headers,
        payload: { confirmToken: previewBody.confirmToken },
      });
      assert.equal(replay.statusCode, 409);
      assert.equal(replay.json().code, "IMPORT_PREVIEW_STALE");

      const second = await app.inject({
        method: "POST",
        url: "/v1/imports/preview",
        headers: { ...headers, "content-type": "application/octet-stream", "x-lyra-file-name": FILE_NAME },
        payload: CSV_CONTENT,
      });
      assert.equal(second.statusCode, 201);
      assert.equal(second.json().preview.insertableRows, 0);

      const cancelled = await app.inject({
        method: "POST",
        url: `/v1/imports/${second.json().importId}/cancel`,
        headers,
      });
      assert.equal(cancelled.statusCode, 200);

      const cancelledJob = await app.inject({
        method: "GET",
        url: `/v1/imports/${second.json().importId}`,
        headers,
      });
      assert.equal(cancelledJob.json().job.status, "cancelled");

      const stagedTransactions = await database.connection.runAndReadAll(`
        SELECT count(*)::INTEGER AS count FROM transactions
      `);
      assert.equal(stagedTransactions.getRowObjectsJson()[0]?.count, 2);

      await app.close();
    } finally {
      await database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
});
