import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createServiceHealthSnapshot } from "../src/application/index.js";

describe("service health snapshot", () => {
  it("数据库和外部集成可用时返回不含敏感详情的健康状态", () => {
    const snapshot = createServiceHealthSnapshot({
      database: { status: "available", schemaVersion: 9, ledgerVersion: 42 },
      writeQueue: { accepting: true, pendingOperations: 2 },
      gmailConnections: [{ status: "active" }, { status: "disconnected" }],
      agentAvailability: {
        checkedAt: "2026-08-31T18:00:00.000Z",
        providers: [
          { provider: "genkit", name: "Lyra", status: "available", version: "Genkit" },
        ],
      },
      checkedAt: new Date("2026-08-31T18:01:00Z"),
    });

    assert.deepEqual(snapshot, {
      status: "healthy",
      checkedAt: "2026-08-31T18:01:00.000Z",
      process: { status: "available" },
      database: { status: "available", schemaVersion: 9, ledgerVersion: 42 },
      writeQueue: { status: "available", pendingOperations: 2 },
      gmail: { status: "available", totalConnections: 2, attentionRequired: 0 },
      agents: { status: "available", availableProviders: 1, unavailableProviders: 0 },
    });
    const serialized = JSON.stringify(snapshot);
    assert.equal(serialized.includes("@"), false);
    assert.equal(serialized.includes("version\":\"2.1.233"), false);
  });

  it("外部故障只降级，而数据库或写队列不可用会阻断 readiness", () => {
    const degraded = createServiceHealthSnapshot({
      database: { status: "available", schemaVersion: 9, ledgerVersion: 42 },
      writeQueue: { accepting: true, pendingOperations: 0 },
      gmailConnections: [{ status: "needs_resync" }],
      agentAvailability: null,
      checkedAt: new Date("2026-08-31T18:02:00Z"),
    });
    assert.equal(degraded.status, "degraded");
    assert.equal(degraded.gmail.attentionRequired, 1);
    assert.equal(degraded.agents.status, "unknown");

    const unavailable = createServiceHealthSnapshot({
      database: { status: "unavailable" },
      writeQueue: { accepting: false, pendingOperations: 1 },
      gmailConnections: [],
      agentAvailability: null,
      checkedAt: new Date("2026-08-31T18:03:00Z"),
    });
    assert.equal(unavailable.status, "unavailable");
    assert.equal(unavailable.process.status, "available");
    assert.equal(unavailable.writeQueue.status, "draining");
  });
});
