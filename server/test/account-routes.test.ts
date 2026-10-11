import assert from "node:assert/strict";
import { generateKeyPairSync, randomBytes, randomUUID } from "node:crypto";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setImmediate } from "node:timers/promises";
import { describe, it, type TestContext } from "node:test";

import {
  AccountService,
  DevicePairingService,
  ImportService,
  WriteCoordinator,
  createDeviceAuthGate,
  createFinancialToolRegistry,
  createIdempotentRequestHash,
  planIdempotentMutation,
} from "../src/application/index.js";
import { GoogleLoginService, pkceChallenge } from "../src/application/google-login-service.js";
import { createBrokerageAccount } from "../src/domain/accounts/index.js";
import { createAuditEvent } from "../src/domain/audit/index.js";
import { persistCreatedBrokerageAccount } from "../src/infrastructure/db/account-repository.js";
import { openLyraDatabase } from "../src/infrastructure/db/database.js";
import { CURRENT_SCHEMA_VERSION } from "../src/infrastructure/db/schema-version.js";
import { registerAccountRoutes } from "../src/transport/account-routes.js";
import { registerGoogleLoginRoutes } from "../src/transport/google-login-routes.js";
import { registerImportRoutes } from "../src/transport/import-routes.js";
import { registerLedgerRoutes } from "../src/transport/ledger-routes.js";
import { buildHttpServer, createServerProtocolMetadata } from "../src/transport/index.js";

const PROTOCOL_HEADERS = { "x-lyra-protocol-version": "2" };
const REQUEST = { displayName: "长期账户", accountIdentifier: "XXX276" };
const FILE_NAME = "Individual_xxx276_Transactions_20260902-080000.csv";
const CSV = Buffer.from([
  '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"',
  '"08/30/2026","Buy","LYRA","LYRA INC","3","$10.00","$0.00","-$30.00"',
  '"08/31/2026","Sell","LYRA","LYRA INC","1","$15.00","$0.00","$15.00"',
  "",
].join("\r\n"));

function newApp() {
  return buildHttpServer({
    protocolMetadata: createServerProtocolMetadata("0.1.0", CURRENT_SCHEMA_VERSION),
    getHealthSnapshot: () => { throw new Error("此测试不调用 health"); },
  });
}

/** 全新 DuckDB；仅模拟 Google 外部验证，设备签发、绑定、鉴权与全部业务 SQL 均真实执行。 */
async function fixture(t: TestContext) {
  const directory = await mkdtemp(join(tmpdir(), "lyra-account-onboarding-"));
  const stagingDirectory = join(directory, "staging");
  await mkdir(stagingDirectory);
  const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
  const writeCoordinator = new WriteCoordinator();
  const now = () => new Date("2026-10-10T14:00:00Z");
  const authenticateDevice = createDeviceAuthGate(database, now);
  const pairingService = new DevicePairingService({ database, writeCoordinator, now });
  const loginService = new GoogleLoginService({
    config: {
      clientId: "test.apps.googleusercontent.com",
      clientSecret: "test-secret",
      redirectUri: "https://lyra.example/v1/auth/google/callback",
      ownerEmail: "owner@example.com",
      ownerSubject: null,
    },
    devicePairingService: pairingService,
    googleClient: {
      authorizationUrl: ({ state }) => `https://accounts.google.com/test?${new URLSearchParams({ state })}`,
      exchangeCode: async () => ({
        provider: "google", subject: "test-owner-subject", email: "owner@example.com", displayName: "Owner",
      }),
    },
    now,
  });
  const registry = createFinancialToolRegistry(database);
  const app = newApp();
  const accountService = new AccountService({ database, writeCoordinator, now });
  registerGoogleLoginRoutes(app, { loginService, pairingService, authenticateDevice });
  registerAccountRoutes(app, { accountService, authenticateDevice });
  registerLedgerRoutes(app, { database, registry, authenticateDevice, now });
  registerImportRoutes(app, {
    importService: new ImportService({ database, writeCoordinator, stagingDirectory, now }),
    authenticateDevice,
  });
  t.after(async () => {
    await app.close();
    await writeCoordinator.close();
    await database.close();
    await rm(directory, { recursive: true, force: true });
  });

  const publicKeyPem = generateKeyPairSync("ed25519").publicKey.export({ type: "spki", format: "pem" }).toString();
  const codeVerifier = randomBytes(32).toString("base64url");
  const start = await app.inject({
    method: "POST", url: "/v1/auth/google/start", headers: PROTOCOL_HEADERS,
    payload: { displayName: "Test Android", publicKeyPem, codeChallenge: pkceChallenge(codeVerifier), codeChallengeMethod: "S256" },
  });
  assert.equal(start.statusCode, 201);
  const started = start.json();
  const state = new URL(started.authorizationUrl).searchParams.get("state")!;
  const callback = await app.inject({
    method: "GET", url: "/v1/auth/google/callback", query: { state, code: "test-google-code" },
  });
  assert.equal(callback.statusCode, 200);
  const exchange = await app.inject({
    method: "POST", url: "/v1/auth/google/exchange", headers: PROTOCOL_HEADERS,
    payload: { loginId: started.loginId, codeVerifier },
  });
  assert.equal(exchange.statusCode, 200);
  assert.equal(exchange.json().status, "complete");
  const credentials = exchange.json();
  const headers = { ...PROTOCOL_HEADERS, authorization: `Bearer ${credentials.accessToken}` };
  return { app, database, writeCoordinator, now, authenticateDevice, credentials, headers, registry };
}

describe("brokerage account creation REST and first import", () => {
  it("从 Google 登录后的空库创建账户、自动匹配 CSV、确认导入并读出真实持仓与已实现", async (t) => {
    const f = await fixture(t);
    const before = await f.app.inject({ method: "GET", url: "/v1/ledger/accounts?scope=all", headers: f.headers });
    assert.equal(before.json().data.count, 0);
    assert.equal(before.json().metadata.ledgerVersion, 0);

    const created = await f.app.inject({
      method: "POST", url: "/v1/ledger/accounts", headers: { ...f.headers, "idempotency-key": randomUUID() },
      payload: { displayName: "  长期账户  ", accountIdentifier: " xxx276 ", baseCurrency: " usd " },
    });
    assert.equal(created.statusCode, 201, created.body);
    assert.equal(created.headers["cache-control"], "no-store");
    const account = created.json().account;
    assert.match(account.id, /^[0-9a-f-]{36}$/u);
    assert.deepEqual(account, {
      id: account.id, displayName: "长期账户", broker: "schwab", maskedSuffix: "276",
      baseCurrency: "USD", status: "active",
      createdAt: "2026-10-10T14:00:00.000Z", updatedAt: "2026-10-10T14:00:00.000Z",
    });
    assert.equal(created.body.includes("XXX276"), false);
    assert.equal("legacyAccountKey" in account, false);
    assert.equal("accountIdentifier" in account, false);
    const listed = await f.app.inject({ method: "GET", url: "/v1/ledger/accounts?scope=all", headers: f.headers });
    assert.deepEqual(listed.json().data.rows, [account]);
    assert.equal(listed.json().metadata.ledgerVersion, 0, "空建账户不能伪造财务版本");
    const initialFinancialRows = await f.database.connection.runAndReadAll(`
      SELECT (SELECT count(*) FROM ledger_versions) AS versions,
        (SELECT count(*) FROM transactions) AS transactions,
        (SELECT count(*) FROM lots) AS lots,
        (SELECT count(*) FROM realized) AS realized
    `);
    assert.deepEqual(initialFinancialRows.getRowObjectsJson(), [{ versions: "0", transactions: "0", lots: "0", realized: "0" }]);

    // No explicit account selection is needed: the filename fragment resolves
    // case-insensitively to the server's canonical, persisted legacy key.
    const preview = await f.app.inject({
      method: "POST", url: "/v1/imports/preview",
      headers: { ...f.headers, "content-type": "application/octet-stream", "x-lyra-file-name": FILE_NAME },
      payload: CSV,
    });
    assert.equal(preview.statusCode, 201, preview.body);
    const previewBody = preview.json();
    assert.equal(previewBody.preview.accountId, account.id);
    assert.equal(previewBody.preview.insertableRows, 2);
    const beforeConfirm = await f.app.inject({ method: "GET", url: `/v1/ledger/positions?scope=${account.id}`, headers: f.headers });
    assert.equal(beforeConfirm.json().data.count, 0, "预览必须保持账本不变");
    assert.equal(beforeConfirm.json().metadata.ledgerVersion, 0);

    const committed = await f.app.inject({
      method: "POST", url: `/v1/imports/${previewBody.importId}/commit`,
      headers: { ...f.headers, "idempotency-key": randomUUID() },
      payload: { confirmToken: previewBody.confirmToken },
    });
    assert.equal(committed.statusCode, 200, committed.body);
    assert.deepEqual(committed.json(), { changed: true, ledgerVersion: 1, insertedRows: 2, skippedRows: 0 });
    const positions = await f.app.inject({ method: "GET", url: `/v1/ledger/positions?scope=${account.id}`, headers: f.headers });
    assert.equal(positions.statusCode, 200);
    assert.equal(positions.json().metadata.ledgerVersion, 1);
    assert.equal(positions.json().data.rows.length, 1);
    assert.equal(positions.json().data.rows[0].accountId, account.id);
    assert.equal(positions.json().data.rows[0].quantity, "2");
    assert.equal(positions.json().data.rows[0].cost, "20");
    const realized = await f.app.inject({ method: "GET", url: `/v1/ledger/realized?scope=${account.id}`, headers: f.headers });
    assert.equal(realized.statusCode, 200);
    assert.equal(realized.json().data.totalPnl, "5.0000");
    assert.equal(realized.json().data.rows[0].accountId, account.id);
    assert.equal(realized.body.includes("XXX276"), false);
    assert.equal(positions.body.includes("XXX276"), false);

    const repeatedPreview = await f.app.inject({
      method: "POST", url: "/v1/imports/preview",
      headers: { ...f.headers, "content-type": "application/octet-stream", "x-lyra-file-name": FILE_NAME.replace("xxx276", "XXX276") },
      payload: CSV,
    });
    assert.equal(repeatedPreview.statusCode, 201, repeatedPreview.body);
    assert.equal(repeatedPreview.json().preview.insertableRows, 0);
    assert.equal(repeatedPreview.json().preview.existingRows, 2, "文件名大小写不改变既有交易哈希");
    const repeatedCommit = await f.app.inject({
      method: "POST", url: `/v1/imports/${repeatedPreview.json().importId}/commit`, headers: f.headers,
      payload: { confirmToken: repeatedPreview.json().confirmToken },
    });
    assert.equal(repeatedCommit.statusCode, 200, repeatedCommit.body);
    assert.deepEqual(repeatedCommit.json(), { changed: false, ledgerVersion: 1, insertedRows: 0, skippedRows: 2 });

    const events = await f.database.connection.runAndReadAll("SELECT * FROM audit_events WHERE event_type = 'account.created'");
    const rows = events.getRowObjectsJson();
    assert.equal(rows.length, 1);
    assert.equal(rows[0]!.actor_id, f.credentials.deviceId);
    assert.equal(rows[0]!.account_id, account.id);
    assert.equal(rows[0]!.ledger_version, null);
    assert.equal(JSON.stringify(rows).includes("XXX276"), false);
    assert.equal(JSON.stringify(rows).includes("长期账户"), false);
    assert.deepEqual(JSON.parse(String(rows[0]!.metadata_json)), { broker: "schwab", base_currency: "USD" });
    assert.equal(f.registry.availableToolDefinitions().length, 18);
    assert.equal(f.registry.availableToolDefinitions().some(({ name }) => name === ("account.create" as string)), false);
  });

  it("拒绝未鉴权、未知字段和非法格式，不回显输入或写入账户", async (t) => {
    const f = await fixture(t);
    for (const headers of [PROTOCOL_HEADERS, { ...PROTOCOL_HEADERS, authorization: "Bearer bad-token" }]) {
      const response = await f.app.inject({ method: "POST", url: "/v1/ledger/accounts", headers, payload: REQUEST });
      assert.equal(response.statusCode, 401);
    }
    const invalidPayloads = [
      {}, null, [],
      { ...REQUEST, displayName: " " },
      { ...REQUEST, displayName: "a".repeat(65) },
      { ...REQUEST, displayName: "line\nbreak" },
      { ...REQUEST, displayName: 42 },
      { ...REQUEST, accountIdentifier: "276" },
      { ...REQUEST, accountIdentifier: "../XXX276" },
      { ...REQUEST, accountIdentifier: "XXX_276" },
      { ...REQUEST, accountIdentifier: "X".repeat(30) + "276" },
      { ...REQUEST, accountIdentifier: "XXXABC" },
      { ...REQUEST, baseCurrency: "US" },
      { ...REQUEST, baseCurrency: null },
      { ...REQUEST, maskedSuffix: "1234" },
      { ...REQUEST, legacyAccountKey: "secret-identifier" },
    ];
    for (const payload of invalidPayloads) {
      const response = await f.app.inject({
        method: "POST", url: "/v1/ledger/accounts", headers: { ...f.headers, "content-type": "application/json" },
        payload: JSON.stringify(payload),
      });
      assert.equal(response.statusCode, 400, response.body);
      assert.equal(response.json().code, "INVALID_ARGUMENT");
      assert.equal(response.body.includes("secret-identifier"), false);
      assert.equal(response.body.includes("XXX276"), false);
    }
    const invalidKey = await f.app.inject({
      method: "POST", url: "/v1/ledger/accounts", headers: { ...f.headers, "idempotency-key": "short" }, payload: REQUEST,
    });
    assert.equal(invalidKey.statusCode, 400);
    const counts = await f.database.connection.runAndReadAll(`
      SELECT (SELECT count(*) FROM accounts) AS accounts,
        (SELECT count(*) FROM mutation_idempotency_records) AS idempotency,
        (SELECT count(*) FROM audit_events) AS audit
    `);
    assert.deepEqual(counts.getRowObjectsJson(), [{ accounts: "0", idempotency: "0", audit: "0" }]);
  });

  it("响应丢失后新 Service 实例从数据库重放同一结果，拒绝同键不同载荷和已归档重复标识", async (t) => {
    const f = await fixture(t);
    const key = randomUUID();
    const headers = { ...f.headers, "idempotency-key": key };
    const created = await f.app.inject({ method: "POST", url: "/v1/ledger/accounts", headers, payload: REQUEST });
    assert.equal(created.statusCode, 201);
    const otherApp = newApp();
    registerAccountRoutes(otherApp, {
      accountService: new AccountService({ database: f.database, writeCoordinator: f.writeCoordinator, now: f.now }),
      authenticateDevice: f.authenticateDevice,
    });
    t.after(() => otherApp.close());
    const replay = await otherApp.inject({
      method: "POST", url: "/v1/ledger/accounts", headers,
      payload: { baseCurrency: "usd", accountIdentifier: " xxx276 ", displayName: " 长期账户 " },
    });
    assert.equal(replay.statusCode, 201, replay.body);
    assert.deepEqual(replay.json(), created.json());
    const reused = await otherApp.inject({
      method: "POST", url: "/v1/ledger/accounts", headers,
      payload: { ...REQUEST, displayName: "变更请求" },
    });
    assert.equal(reused.statusCode, 409);
    assert.equal(reused.json().code, "IDEMPOTENCY_KEY_REUSED");

    await f.writeCoordinator.enqueue(() => f.database.connection.run("UPDATE accounts SET status = 'archived'"));
    const duplicate = await otherApp.inject({
      method: "POST", url: "/v1/ledger/accounts", headers: f.headers,
      payload: { ...REQUEST, accountIdentifier: "xxx276" },
    });
    assert.equal(duplicate.statusCode, 409);
    assert.equal(duplicate.json().code, "ACCOUNT_ALREADY_EXISTS");
    assert.equal(duplicate.body.includes("XXX276"), false);
    const counts = await f.database.connection.runAndReadAll(`
      SELECT (SELECT count(*) FROM accounts) AS accounts,
        (SELECT count(*) FROM mutation_idempotency_records WHERE status = 'completed') AS idempotency,
        (SELECT count(*) FROM audit_events WHERE event_type = 'account.created') AS audit
    `);
    assert.deepEqual(counts.getRowObjectsJson(), [{ accounts: "1", idempotency: "1", audit: "1" }]);
    const records = await f.database.connection.runAndReadAll("SELECT * FROM mutation_idempotency_records");
    assert.equal(JSON.stringify(records.getRowObjectsJson()).includes(key), false);
    assert.equal(JSON.stringify(records.getRowObjectsJson()).includes("XXX276"), false);
    const logout = await f.app.inject({ method: "POST", url: "/v1/auth/logout", headers: f.headers });
    assert.equal(logout.statusCode, 204);
    const replayAfterLogout = await otherApp.inject({ method: "POST", url: "/v1/ledger/accounts", headers, payload: REQUEST });
    assert.equal(replayAfterLogout.statusCode, 401, "已完成的幂等结果也要求当前有效身份");
  });

  it("异幂等键并发创建同标识只有一个成功，同键并发重试只产生一个账户", async (t) => {
    const f = await fixture(t);
    const responses = await Promise.all(["XXX276", "xxx276"].map((accountIdentifier) => f.app.inject({
      method: "POST", url: "/v1/ledger/accounts", headers: { ...f.headers, "idempotency-key": randomUUID() },
      payload: { ...REQUEST, accountIdentifier },
    })));
    assert.deepEqual(responses.map((response) => response.statusCode).sort(), [201, 409]);
    assert.equal(responses.find((response) => response.statusCode === 409)!.json().code, "ACCOUNT_ALREADY_EXISTS");
    const headers = { ...f.headers, "idempotency-key": randomUUID() };
    const sameKey = await Promise.all(Array.from({ length: 3 }, () => f.app.inject({
      method: "POST", url: "/v1/ledger/accounts", headers, payload: { ...REQUEST, accountIdentifier: "XXX1234" },
    })));
    assert.deepEqual(sameKey.map((response) => response.statusCode), [201, 201, 201]);
    assert.deepEqual(sameKey[0]!.json(), sameKey[1]!.json());
    assert.deepEqual(sameKey[1]!.json(), sameKey[2]!.json());
    assert.equal(sameKey[0]!.json().account.maskedSuffix, "1234");
    const counts = await f.database.connection.runAndReadAll(`
      SELECT (SELECT count(*) FROM accounts) AS accounts,
        (SELECT count(*) FROM mutation_idempotency_records WHERE status = 'completed') AS idempotency,
        (SELECT count(*) FROM mutation_idempotency_records WHERE status = 'in_progress') AS pending,
        (SELECT count(*) FROM audit_events WHERE event_type = 'account.created') AS audit,
        (SELECT count(*) FROM ledger_versions) AS versions
    `);
    assert.deepEqual(counts.getRowObjectsJson(), [{ accounts: "2", idempotency: "2", pending: "0", audit: "2", versions: "0" }]);
  });

  it("已通过入口鉴权但排在退出登录后的创建被重新鉴权拒绝，队列没有任何账户副作用", async (t) => {
    const f = await fixture(t);
    let release!: () => void;
    const held = f.writeCoordinator.enqueue(() => new Promise<void>((resolve) => { release = resolve; }));
    const logout = f.app.inject({ method: "POST", url: "/v1/auth/logout", headers: f.headers }).then((response) => response);
    try {
      await waitFor(() => f.writeCoordinator.status().pendingOperations === 2);
      const create = f.app.inject({
        method: "POST", url: "/v1/ledger/accounts", headers: { ...f.headers, "idempotency-key": randomUUID() }, payload: REQUEST,
      }).then((response) => response);
      await waitFor(() => f.writeCoordinator.status().pendingOperations === 3);
      release();
      await held;
      assert.equal((await logout).statusCode, 204);
      const denied = await create;
      assert.equal(denied.statusCode, 401, denied.body);
      assert.ok(["DEVICE_REVOKED", "DEVICE_CREDENTIAL_INVALID"].includes(denied.json().code));
      const counts = await f.database.connection.runAndReadAll(`
        SELECT (SELECT count(*) FROM accounts) AS accounts,
          (SELECT count(*) FROM mutation_idempotency_records) AS idempotency,
          (SELECT count(*) FROM audit_events) AS audit,
          (SELECT count(*) FROM ledger_versions) AS versions
      `);
      assert.deepEqual(counts.getRowObjectsJson(), [{ accounts: "0", idempotency: "0", audit: "0", versions: "0" }]);
    } finally {
      release();
      await held;
      await logout;
    }
  });

  it("审计落库失败时账户和幂等开始记录一起回滚，同一请求之后可以成功重试", async (t) => {
    const f = await fixture(t);
    const key = randomUUID();
    const now = f.now();
    const account = createBrokerageAccount({
      displayName: REQUEST.displayName, legacyAccountKey: REQUEST.accountIdentifier, maskedSuffix: "276",
    }, randomUUID(), now);
    const idempotency = planIdempotentMutation({
      recordId: randomUUID(), deviceId: f.credentials.deviceId, operation: "account.create", key,
      requestHash: createIdempotentRequestHash("account.create", { ...REQUEST, baseCurrency: "USD" }),
      existingRecord: null, now,
    });
    assert.equal(idempotency.kind, "start");
    if (idempotency.kind !== "start") throw new Error("首次请求必须开始");
    // A missing referenced account forces a real DuckDB constraint failure after
    // the new account and in-progress idempotency row have both been inserted.
    const auditEvent = createAuditEvent({
      eventType: "account.created", outcome: "success", actorType: "device",
      actorId: f.credentials.deviceId, accountId: randomUUID(), now,
    });
    await assert.rejects(f.writeCoordinator.enqueue(() => persistCreatedBrokerageAccount(f.database, {
      account, auditEvent, idempotency,
    })));
    const counts = await f.database.connection.runAndReadAll(`
      SELECT (SELECT count(*) FROM accounts) AS accounts,
        (SELECT count(*) FROM mutation_idempotency_records) AS idempotency,
        (SELECT count(*) FROM audit_events) AS audit,
        (SELECT count(*) FROM ledger_versions) AS versions
    `);
    assert.deepEqual(counts.getRowObjectsJson(), [{ accounts: "0", idempotency: "0", audit: "0", versions: "0" }]);
    const retried = await f.app.inject({
      method: "POST", url: "/v1/ledger/accounts", headers: { ...f.headers, "idempotency-key": key }, payload: REQUEST,
    });
    assert.equal(retried.statusCode, 201, retried.body);
  });
});

async function waitFor(predicate: () => boolean): Promise<void> {
  const deadline = Date.now() + 5_000;
  while (!predicate()) {
    if (Date.now() > deadline) {
      throw new Error("预期写队列状态没有出现");
    }
    await setImmediate();
  }
}
