import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import {
  ImportService,
  WriteCoordinator,
  createDeviceAuthGate,
  createIdempotentRequestHash,
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

  it("提交支持 Idempotency-Key 重放且失败释放不阻塞重试", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-import-idem-"));
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
      const commitKey = "import-commit-key-000001";
      const commitHeaders = { ...headers, "idempotency-key": commitKey };

      // 非法幂等键在进入业务前明确拒绝。
      const badKey = await app.inject({
        method: "POST",
        url: "/v1/imports/9f100000-0000-4000-8000-000000000001/commit",
        headers: { ...headers, "idempotency-key": "short" },
        payload: { confirmToken: "whatever" },
      });
      assert.equal(badKey.statusCode, 400);
      assert.equal(badKey.json().code, "INVALID_ARGUMENT");

      const preview = await app.inject({
        method: "POST",
        url: "/v1/imports/preview",
        headers: { ...headers, "content-type": "application/octet-stream", "x-lyra-file-name": FILE_NAME },
        payload: CSV_CONTENT,
      });
      assert.equal(preview.statusCode, 201);
      const importId = preview.json().importId as string;
      const confirmToken = preview.json().confirmToken as string;

      // 业务事务内失败：in_progress 幂等记录被释放，同一逻辑可用同键重试。
      // 直接注入新账本版本制造 LEDGER_VERSION_CONFLICT，失败后移除注入恢复现场。
      await database.connection.run(`
        INSERT INTO ledger_versions (reason, source_reference)
        VALUES ('fifo_rebuild', 'idempotency-release-test')
      `);
      const failed = await app.inject({
        method: "POST",
        url: `/v1/imports/${importId}/commit`,
        headers: commitHeaders,
        payload: { confirmToken },
      });
      assert.equal(failed.statusCode, 409);
      assert.equal(failed.json().code, "LEDGER_VERSION_CONFLICT");
      const afterFailure = await database.connection.runAndReadAll(`
        SELECT count(*)::INTEGER AS count FROM mutation_idempotency_records
      `);
      assert.equal(afterFailure.getRowObjectsJson()[0]?.count, 0);
      await database.connection.run(`
        DELETE FROM ledger_versions
        WHERE reason = 'fifo_rebuild'
          AND source_reference = 'idempotency-release-test'
      `);

      // 带幂等键首次提交成功并完成记录；注入冲突时消耗过一个版本号，
      // 序列不回退，因此实际账本版本为 2。
      const commit = await app.inject({
        method: "POST",
        url: `/v1/imports/${importId}/commit`,
        headers: commitHeaders,
        payload: { confirmToken },
      });
      assert.equal(commit.statusCode, 200);
      assert.deepEqual(commit.json(), {
        changed: true,
        ledgerVersion: 2,
        insertedRows: 2,
        skippedRows: 0,
      });
      const completedRecord = await database.connection.runAndReadAll(`
        SELECT status, result_resource_id, response_status_code
        FROM mutation_idempotency_records
      `);
      assert.equal(completedRecord.getRowObjectsJson().length, 1);
      const record = completedRecord.getRowObjectsJson()[0]!;
      assert.equal(record.status, "completed");
      assert.equal(record.result_resource_id, importId);
      assert.equal(record.response_status_code, 200);

      // 相同键重放相同请求返回持久化结果；此时进程内解析结果已被丢弃，
      // 证明重放路径只依赖持久化幂等记录与导入作业状态。
      const replay = await app.inject({
        method: "POST",
        url: `/v1/imports/${importId}/commit`,
        headers: commitHeaders,
        payload: { confirmToken },
      });
      assert.equal(replay.statusCode, 200);
      assert.deepEqual(replay.json(), {
        changed: true,
        ledgerVersion: 2,
        insertedRows: 2,
        skippedRows: 0,
      });

      // 相同键携带不同令牌（不同请求）明确拒绝，不返回错误资源的结果。
      const conflicting = await app.inject({
        method: "POST",
        url: `/v1/imports/${importId}/commit`,
        headers: commitHeaders,
        payload: { confirmToken: `${confirmToken}-different` },
      });
      assert.equal(conflicting.statusCode, 409);
      assert.equal(conflicting.json().code, "IDEMPOTENCY_KEY_REUSED");

      // 处理中状态对并发同键请求返回可重试冲突；键与请求指纹必须与真实
      // 请求完全一致，模拟的是"同一请求仍在处理中"的并发竞态。
      const pendingKey = "import-commit-key-000002";
      const pendingHeaders = { ...headers, "idempotency-key": pendingKey };
      const secondPreview = await app.inject({
        method: "POST",
        url: "/v1/imports/preview",
        headers: { ...headers, "content-type": "application/octet-stream", "x-lyra-file-name": FILE_NAME },
        payload: CSV_CONTENT,
      });
      assert.equal(secondPreview.statusCode, 201);
      const secondImportId = secondPreview.json().importId as string;
      const secondToken = secondPreview.json().confirmToken as string;
      const pendingKeyHash = createHash("sha256")
        .update(pendingKey, "utf8").digest("hex");
      const pendingRequestHash = createIdempotentRequestHash("import.commit", {
        importId: secondImportId,
        confirmToken: secondToken,
      });
      // 过期时间必须晚于服务端真实时钟，才会走"处理中"分支而不是过期替换。
      const pendingCreatedAt = new Date(Date.now() - 60_000).toISOString();
      const pendingExpiresAt = new Date(Date.now() + 24 * 60 * 60 * 1000)
        .toISOString();
      await database.connection.run(`
        INSERT INTO mutation_idempotency_records (
          id, device_id, operation, key_hash, request_hash, status,
          created_at, expires_at
        ) VALUES (
          CAST($id AS UUID), CAST($device_id AS UUID), 'import.commit', $key_hash,
          $request_hash, 'in_progress',
          CAST($created_at AS TIMESTAMPTZ), CAST($expires_at AS TIMESTAMPTZ)
        )
      `, {
        id: "9f200000-0000-4000-8000-000000000001",
        device_id: DEVICE_ID,
        key_hash: pendingKeyHash,
        request_hash: pendingRequestHash,
        created_at: pendingCreatedAt,
        expires_at: pendingExpiresAt,
      });
      const blocked = await app.inject({
        method: "POST",
        url: `/v1/imports/${secondImportId}/commit`,
        headers: pendingHeaders,
        payload: { confirmToken: secondToken },
      });
      assert.equal(blocked.statusCode, 409);
      assert.equal(blocked.json().code, "IDEMPOTENCY_REQUEST_IN_PROGRESS");
      assert.equal(blocked.json().retryable, true);

      await app.close();
    } finally {
      await database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
});
