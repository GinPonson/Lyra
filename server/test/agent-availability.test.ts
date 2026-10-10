import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { createAgentAvailabilitySnapshot } from "../src/application/index.js";

describe("single Lyra availability", () => {
  it("publishes one Genkit assistant without model endpoint or key", () => {
    assert.deepEqual(createAgentAvailabilitySnapshot(true, new Date("2026-10-10T10:00:00Z")), {
      checkedAt: "2026-10-10T10:00:00.000Z",
      providers: [{ provider: "genkit", name: "Lyra", status: "available", version: "Genkit" }],
    });
    assert.deepEqual(createAgentAvailabilitySnapshot(false, new Date()).providers, [
      { provider: "genkit", name: "Lyra", status: "unavailable", reason: "not_configured" },
    ]);
    assert.throws(() => createAgentAvailabilitySnapshot(true, new Date("invalid")), RangeError);
  });
});
