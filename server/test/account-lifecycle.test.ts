import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  archiveBrokerageAccount,
  createBrokerageAccount,
  renameBrokerageAccount,
} from "../src/domain/accounts/index.js";

describe("brokerage account lifecycle", () => {
  it("创建时规范化可编辑字段并使用 USD 默认币种", () => {
    const account = createBrokerageAccount({
      displayName: "  长期账户  ",
      legacyAccountKey: " XXX276 ",
      maskedSuffix: " x276 ",
    }, "account-id", new Date("2026-08-30T01:02:03Z"));

    assert.deepEqual(account, {
      id: "account-id",
      displayName: "长期账户",
      legacyAccountKey: "XXX276",
      broker: "schwab",
      maskedSuffix: "X276",
      baseCurrency: "USD",
      status: "active",
      createdAt: "2026-08-30T01:02:03.000Z",
      updatedAt: "2026-08-30T01:02:03.000Z",
    });
  });

  it("重命名只改变显示字段，归档保留身份且重复调用幂等", () => {
    const created = createBrokerageAccount({
      displayName: "主账户",
      legacyAccountKey: "XXX276",
      maskedSuffix: "276",
      baseCurrency: "usd",
    }, "account-id", new Date("2026-08-30T00:00:00Z"));
    const renamed = renameBrokerageAccount(
      created,
      "  退休账户  ",
      new Date("2026-08-30T01:00:00Z"),
    );
    const archived = archiveBrokerageAccount(
      renamed,
      new Date("2026-08-30T02:00:00Z"),
    );

    assert.equal(renamed.displayName, "退休账户");
    assert.equal(renamed.legacyAccountKey, created.legacyAccountKey);
    assert.equal(archived.status, "archived");
    assert.equal(archived.updatedAt, "2026-08-30T02:00:00.000Z");
    assert.equal(
      archiveBrokerageAccount(archived, new Date("2026-08-31T00:00:00Z")),
      archived,
    );
    assert.throws(
      () => createBrokerageAccount({
        displayName: "非法币种",
        legacyAccountKey: "XXX999",
        maskedSuffix: "999",
        baseCurrency: "US",
      }, "account-invalid", new Date("2026-08-30T00:00:00Z")),
      RangeError,
    );
  });
});
