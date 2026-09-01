import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  ApplicationError,
  assertImportPreviewAuthorized,
  issueImportPreviewAuthorization,
} from "../src/application/index.js";

describe("import preview authorization", () => {
  it("只返回一次明文令牌并验证其绑定的预览与作业状态", () => {
    const issued = issueImportPreviewAuthorization(
      "import-1",
      "a".repeat(64),
      new Date("2026-08-30T00:00:00Z"),
      60_000,
    );
    assert.match(issued.token, /^[A-Za-z0-9_-]{43}$/);
    assert.equal("token" in issued.record, false);
    assert.doesNotThrow(() => assertImportPreviewAuthorized({
      record: issued.record,
      importId: "import-1",
      previewFingerprint: "a".repeat(64),
      token: issued.token,
      jobStatus: "preview_ready",
      now: new Date("2026-08-30T00:00:59Z"),
    }));
  });

  it("用稳定失效错误拒绝错误令牌、过期或状态变化", () => {
    const issued = issueImportPreviewAuthorization(
      "import-1",
      "a".repeat(64),
      new Date("2026-08-30T00:00:00Z"),
      60_000,
    );
    const invalidCases = [
      { token: "wrong", now: new Date("2026-08-30T00:00:30Z"), jobStatus: "preview_ready" as const },
      { token: issued.token, now: new Date("2026-08-30T00:01:00Z"), jobStatus: "preview_ready" as const },
      { token: issued.token, now: new Date("2026-08-30T00:00:30Z"), jobStatus: "committing" as const },
    ];
    for (const invalid of invalidCases) {
      assert.throws(
        () => assertImportPreviewAuthorized({
          record: issued.record,
          importId: "import-1",
          previewFingerprint: "a".repeat(64),
          ...invalid,
        }),
        (error: unknown) => {
          assert.ok(error instanceof ApplicationError);
          assert.equal(error.code, "IMPORT_PREVIEW_STALE");
          assert.equal(error.statusCode, 409);
          return true;
        },
      );
    }
  });
});
