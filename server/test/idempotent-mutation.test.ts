import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { describe, it } from "node:test";

import {
  ApplicationError,
  completeIdempotentMutation,
  createIdempotentRequestHash,
  planIdempotentMutation,
  type MutationIdempotencyRecord,
} from "../src/application/index.js";

describe("device-scoped mutation idempotency", () => {
  it("规范化请求指纹并只持久化幂等键哈希和结果引用", () => {
    const key = "018f6a90-55e4-7e3a-8000-000000000001";
    const firstHash = createIdempotentRequestHash("account.rename", {
      accountId: "account-1",
      input: { currency: "USD", displayName: "Growth" },
    });
    const reorderedHash = createIdempotentRequestHash("account.rename", {
      input: { displayName: "Growth", currency: "USD" },
      accountId: "account-1",
    });
    assert.equal(firstHash, reorderedHash);

    const plan = planIdempotentMutation({
      recordId: "record-1",
      deviceId: "device-1",
      operation: "account.rename",
      key,
      requestHash: firstHash,
      existingRecord: null,
      now: new Date("2026-08-31T08:00:00Z"),
    });
    assert.equal(plan.kind, "start");
    if (plan.kind !== "start") {
      throw new Error("首次幂等请求应进入 start");
    }
    assert.equal(
      plan.record.keyHash,
      createHash("sha256").update(key, "utf8").digest("hex"),
    );
    assert.equal(JSON.stringify(plan).includes(key), false);
    assert.equal(plan.record.expiresAt, "2026-09-01T08:00:00.000Z");

    const completed = completeIdempotentMutation(plan.record, {
      resultResourceId: "account-1",
      responseStatusCode: 200,
      now: new Date("2026-08-31T08:00:01Z"),
    });
    assert.deepEqual(planIdempotentMutation({
      recordId: "unused-record",
      deviceId: "device-1",
      operation: "account.rename",
      key,
      requestHash: firstHash,
      existingRecord: completed,
      now: new Date("2026-08-31T08:05:00Z"),
    }), {
      kind: "replay",
      recordId: "record-1",
      resultResourceId: "account-1",
      responseStatusCode: 200,
      completedAt: "2026-08-31T08:00:01.000Z",
    });
  });

  it("拒绝不同请求复用有效键，并允许原子替换过期记录", () => {
    const key = "018f6a90-55e4-7e3a-8000-000000000002";
    const originalHash = createIdempotentRequestHash("account.rename", {
      displayName: "Growth",
    });
    const existing: MutationIdempotencyRecord = {
      id: "record-old",
      deviceId: "device-1",
      operation: "account.rename",
      keyHash: createHash("sha256").update(key, "utf8").digest("hex"),
      requestHash: originalHash,
      status: "in_progress",
      resultResourceId: null,
      responseStatusCode: null,
      createdAt: "2026-08-30T08:00:00.000Z",
      expiresAt: "2026-08-31T08:00:00.000Z",
      completedAt: null,
    };

    assert.throws(
      () => planIdempotentMutation({
        recordId: "record-new",
        deviceId: "device-1",
        operation: "account.rename",
        key,
        requestHash: createIdempotentRequestHash("account.rename", {
          displayName: "Income",
        }),
        existingRecord: existing,
        now: new Date("2026-08-31T07:59:00Z"),
      }),
      (error: unknown) => error instanceof ApplicationError
        && error.code === "IDEMPOTENCY_KEY_REUSED"
        && error.statusCode === 409
        && !error.retryable,
    );

    const replacement = planIdempotentMutation({
      recordId: "record-new",
      deviceId: "device-1",
      operation: "account.rename",
      key,
      requestHash: createIdempotentRequestHash("account.rename", {
        displayName: "Income",
      }),
      existingRecord: existing,
      now: new Date("2026-08-31T08:00:00Z"),
    });
    assert.equal(replacement.kind, "start");
    if (replacement.kind !== "start") {
      throw new Error("过期幂等记录应允许替换");
    }
    assert.equal(replacement.replacesExpiredRecordId, "record-old");
    assert.equal(replacement.record.id, "record-new");
  });
});
