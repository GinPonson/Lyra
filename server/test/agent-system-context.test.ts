import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  buildAgentSystemContext,
} from "../src/domain/agent/index.js";
import type { FinancialToolName } from "../src/domain/agent/index.js";

describe("Agent financial system context", () => {
  it("固化授权口径、工具来源和金融回答披露要求", () => {
    const context = buildAgentSystemContext({
      accountScope: { kind: "account", account: "TST276" },
      ledgerVersion: 18,
      asOf: "2026-08-31T04:00:00Z",
      availableTools: ["positions", "summary", "audit"],
    });

    assert.match(context, /单账户 TST276/);
    assert.match(context, /账本版本 18/);
    assert.match(context, /summary, positions, audit/);
    assert.match(context, /账户范围、截止时间、账本版本、计算口径和实际使用的工具/);
    assert.match(context, /历史成本不是当前市值/);
    assert.match(context, /不得导入数据/);
    assert.equal(context.includes("邮件正文"), false);
  });

  it("拒绝非法账本版本、空截止时间和运行时注入的非白名单工具", () => {
    assert.throws(
      () => buildAgentSystemContext({
        accountScope: { kind: "all" },
        ledgerVersion: -1,
        asOf: "2026-08-31",
        availableTools: [],
      }),
      RangeError,
    );
    assert.throws(
      () => buildAgentSystemContext({
        accountScope: { kind: "all" },
        ledgerVersion: 1,
        asOf: " ",
        availableTools: [],
      }),
      RangeError,
    );
    assert.throws(
      () => buildAgentSystemContext({
        accountScope: { kind: "all" },
        ledgerVersion: 1,
        asOf: "2026-08-31",
        availableTools: ["shell" as FinancialToolName],
      }),
      RangeError,
    );
  });
});
