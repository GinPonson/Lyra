import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createPublicAccountList } from "../src/application/index.js";
import type { BrokerageAccount } from "../src/domain/accounts/index.js";

const ACCOUNTS: readonly BrokerageAccount[] = [
  {
    id: "account-archived",
    displayName: "旧账户",
    legacyAccountKey: "FULL-LEGACY-9999",
    broker: "schwab",
    maskedSuffix: "9999",
    baseCurrency: "USD",
    status: "archived",
    createdAt: "2025-01-01T00:00:00.000Z",
    updatedAt: "2026-01-01T00:00:00.000Z",
  },
  {
    id: "account-new",
    displayName: "交易账户",
    legacyAccountKey: "FULL-LEGACY-2222",
    broker: "schwab",
    maskedSuffix: "2222",
    baseCurrency: "USD",
    status: "active",
    createdAt: "2026-01-01T00:00:00.000Z",
    updatedAt: "2026-01-01T00:00:00.000Z",
  },
  {
    id: "account-old",
    displayName: "退休账户",
    legacyAccountKey: "FULL-LEGACY-1111",
    broker: "schwab",
    maskedSuffix: "1111",
    baseCurrency: "USD",
    status: "active",
    createdAt: "2025-06-01T00:00:00.000Z",
    updatedAt: "2025-06-01T00:00:00.000Z",
  },
];

describe("public account list", () => {
  it("稳定排序账户且不向 App 或 Agent 暴露迁移期旧账户键", () => {
    const result = createPublicAccountList(ACCOUNTS);

    assert.deepEqual(result.map((account) => account.id), [
      "account-old",
      "account-new",
      "account-archived",
    ]);
    assert.deepEqual(result.map((account) => account.maskedSuffix), ["1111", "2222", "9999"]);
    assert.equal(result.some((account) => "legacyAccountKey" in account), false);
    assert.equal(JSON.stringify(result).includes("FULL-LEGACY"), false);
    assert.deepEqual(ACCOUNTS.map((account) => account.id), [
      "account-archived",
      "account-new",
      "account-old",
    ]);
  });
});
