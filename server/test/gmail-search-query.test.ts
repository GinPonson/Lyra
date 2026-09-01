import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { buildSchwabEconfirmSearch } from "../src/infrastructure/gmail/index.js";

describe("bounded Gmail eConfirm search", () => {
  it("固定已确认发件人与主题并绑定用户确认的 UTC 起始日和消息上限", () => {
    const plan = buildSchwabEconfirmSearch("2026-01-01", 500);

    assert.deepEqual(plan, {
      query: (
        "from:donotreply@mail.schwab.com "
        + "subject:\"Schwab eConfirms\" "
        + "after:1767225600"
      ),
      afterDate: "2026-01-01",
      maximumMessages: 500,
    });
  });

  it("拒绝非法日期和非正扫描上限", () => {
    assert.throws(() => buildSchwabEconfirmSearch("2026-02-30", 500), RangeError);
    assert.throws(() => buildSchwabEconfirmSearch("2026-01-01", 0), RangeError);
  });
});
