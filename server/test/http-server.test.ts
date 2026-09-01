import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createServiceHealthSnapshot } from "../src/application/index.js";
import {
  buildHttpServer,
  createServerProtocolMetadata,
} from "../src/transport/index.js";

/** 构造测试所需的稳定健康快照，避免复制服务端状态规则。 */
function createHealthySnapshot() {
  return createServiceHealthSnapshot({
    database: { status: "available", schemaVersion: 10, ledgerVersion: 42 },
    writeQueue: { accepting: true, pendingOperations: 0 },
    gmailConnections: [],
    agentAvailability: {
      checkedAt: "2026-08-31T08:30:00.000Z",
      providers: [
        { provider: "claude", status: "available", version: "2.1.233" },
        { provider: "pi", status: "available", version: "0.84.2" },
        { provider: "opencode", status: "available", version: "1.18.21" },
      ],
    },
    checkedAt: new Date("2026-08-31T08:31:00.000Z"),
  });
}

describe("Fastify vertical API", () => {
  it("公开协议握手与健康快照，并为响应附加关联 ID", async () => {
    const app = buildHttpServer({
      protocolMetadata: createServerProtocolMetadata("0.1.0", 10),
      getHealthSnapshot: createHealthySnapshot,
    });

    const info = await app.inject({ method: "GET", url: "/v1/system/info" });
    assert.equal(info.statusCode, 200);
    assert.deepEqual(info.json(), {
      server_version: "0.1.0",
      protocol_version: 1,
      min_supported_protocol_version: 1,
      max_supported_protocol_version: 1,
      schema_version: 10,
    });
    assert.match(String(info.headers["x-request-id"] ?? ""), /^[0-9a-f-]{36}$/u);

    const health = await app.inject({ method: "GET", url: "/v1/health" });
    assert.equal(health.statusCode, 200);
    assert.equal(health.json().status, "healthy");
    assert.equal(JSON.stringify(health.json()).includes("2.1.233"), false);

    await app.close();
  });

  it("对后续 v1 路由执行协议门禁并返回统一错误正文", async () => {
    const app = buildHttpServer({
      protocolMetadata: createServerProtocolMetadata("0.1.0", 10),
      getHealthSnapshot: createHealthySnapshot,
    });
    app.get("/v1/test-protected", async () => ({ ok: true }));

    const rejected = await app.inject({
      method: "GET",
      url: "/v1/test-protected",
    });
    assert.equal(rejected.statusCode, 426);
    assert.equal(rejected.json().code, "CLIENT_VERSION_INCOMPATIBLE");
    assert.equal(rejected.json().request_id, rejected.headers["x-request-id"]);

    const accepted = await app.inject({
      method: "GET",
      url: "/v1/test-protected",
      headers: { "x-lyra-protocol-version": "1" },
    });
    assert.equal(accepted.statusCode, 200);
    assert.deepEqual(accepted.json(), { ok: true });

    await app.close();
  });

  it("健康状态不可用时返回 503，且未知异常不泄露敏感信息", async () => {
    const app = buildHttpServer({
      protocolMetadata: createServerProtocolMetadata("0.1.0", 10),
      getHealthSnapshot: () => createServiceHealthSnapshot({
        database: { status: "unavailable" },
        writeQueue: { accepting: false, pendingOperations: 0 },
        gmailConnections: [],
        agentAvailability: null,
        checkedAt: new Date("2026-08-31T08:32:00.000Z"),
      }),
    });
    app.get("/v1/test-error", async () => {
      throw new Error("token=secret path=/private/ledger.duckdb");
    });

    const health = await app.inject({ method: "GET", url: "/v1/health" });
    assert.equal(health.statusCode, 503);
    assert.equal(health.json().status, "unavailable");

    const failed = await app.inject({
      method: "GET",
      url: "/v1/test-error",
      headers: { "x-lyra-protocol-version": "1" },
    });
    const serialized = failed.body;
    assert.equal(failed.statusCode, 500);
    assert.equal(failed.json().code, "INTERNAL_ERROR");
    assert.equal(serialized.includes("secret"), false);
    assert.equal(serialized.includes("ledger.duckdb"), false);

    await app.close();
  });
});
