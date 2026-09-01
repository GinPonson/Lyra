import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createAgentAvailabilitySnapshot } from "../src/application/index.js";
import type { AgentCliProbeResult } from "../src/infrastructure/agents/index.js";

const RESULTS: readonly AgentCliProbeResult[] = [
  { provider: "opencode", status: "available", version: "1.18.21" },
  { provider: "claude", status: "available", version: "2.1.233" },
  { provider: "pi", status: "unavailable", reason: "EXECUTABLE_NOT_FOUND" },
];

describe("Agent availability snapshot", () => {
  it("按固定供应商顺序公开版本或稳定失败原因", () => {
    const snapshot = createAgentAvailabilitySnapshot(
      RESULTS,
      new Date("2026-08-31T13:00:00Z"),
    );

    assert.deepEqual(snapshot, {
      checkedAt: "2026-08-31T13:00:00.000Z",
      providers: [
        { provider: "claude", status: "available", version: "2.1.233" },
        { provider: "pi", status: "unavailable", reason: "EXECUTABLE_NOT_FOUND" },
        { provider: "opencode", status: "available", version: "1.18.21" },
      ],
    });
    assert.equal(JSON.stringify(snapshot).includes("executablePath"), false);
    assert.equal(JSON.stringify(snapshot).includes("stderr"), false);
  });

  it("拒绝供应商探测结果缺失、重复或完成时间无效", () => {
    assert.throws(
      () => createAgentAvailabilitySnapshot(RESULTS.slice(0, 2), new Date()),
      RangeError,
    );
    assert.throws(
      () => createAgentAvailabilitySnapshot(
        [...RESULTS, RESULTS[0]!],
        new Date(),
      ),
      RangeError,
    );
    assert.throws(
      () => createAgentAvailabilitySnapshot(RESULTS, new Date("invalid")),
      RangeError,
    );
  });
});
