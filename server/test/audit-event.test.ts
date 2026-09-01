import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { AuditEventError, createAuditEvent } from "../src/domain/audit/index.js";

describe("audit event policy", () => {
  it("建立可关联请求、设备、账户和账本版本的最小审计事件", () => {
    const event = createAuditEvent({
      eventType: "import.committed",
      outcome: "success",
      actorType: "device",
      actorId: "device-1",
      requestId: "request-1",
      targetType: "import",
      targetId: "import-1",
      accountId: "account-1",
      ledgerVersion: 12,
      metadata: { inserted_rows: 48, source_type: "excel" },
      now: new Date("2026-08-30T00:00:00Z"),
    });

    assert.match(event.id, /^[a-f0-9-]{36}$/);
    assert.equal(event.occurredAt, "2026-08-30T00:00:00.000Z");
    assert.equal(event.actorId, "device-1");
    assert.deepEqual(event.metadata, { inserted_rows: 48, source_type: "excel" });
  });

  it("拒绝敏感元数据和缺少错误码的失败事件", () => {
    assert.throws(
      () => createAuditEvent({
        eventType: "gmail.authorized",
        outcome: "success",
        actorType: "system",
        metadata: { refresh_token: "must-not-be-stored" },
        now: new Date("2026-08-30T00:00:00Z"),
      }),
      AuditEventError,
    );
    assert.throws(
      () => createAuditEvent({
        eventType: "auth.failed",
        outcome: "failure",
        actorType: "system",
        now: new Date("2026-08-30T00:00:00Z"),
      }),
      AuditEventError,
    );
  });
});
