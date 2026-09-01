import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  ApplicationError,
  transitionImportJob,
  type ImportJobState,
  type ImportJobStatus,
} from "../src/application/index.js";

describe("import job state machine", () => {
  it("只允许完整预览后进入提交并到达终态", () => {
    let state: ImportJobState = {
      importId: "import-1",
      status: "uploaded",
      updatedAt: "2026-08-30T00:00:00.000Z",
    };
    for (const [index, status] of ([
      "parsing",
      "preview_ready",
      "committing",
      "committed",
    ] as const).entries()) {
      state = transitionImportJob(
        state,
        status,
        new Date(`2026-08-30T00:0${index + 1}:00Z`),
      );
    }
    assert.equal(state.status, "committed");
    assert.equal(state.updatedAt, "2026-08-30T00:04:00.000Z");
    assert.equal(transitionImportJob(state, "committed", new Date()), state);
  });

  it("拒绝绕过预览、终态回退和提交期间取消", () => {
    const invalidTransitions: readonly [ImportJobStatus, ImportJobStatus][] = [
      ["uploaded", "committing"],
      ["committing", "cancelled"],
      ["committed", "parsing"],
    ];
    for (const [currentStatus, nextStatus] of invalidTransitions) {
      assert.throws(
        () => transitionImportJob({
          importId: "import-1",
          status: currentStatus,
          updatedAt: "2026-08-30T00:00:00.000Z",
        }, nextStatus, new Date("2026-08-30T00:01:00Z")),
        (error: unknown) => {
          assert.ok(error instanceof ApplicationError);
          assert.equal(error.code, "IMPORT_STATE_CONFLICT");
          assert.equal(error.statusCode, 409);
          return true;
        },
      );
    }
  });
});
