import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  ApplicationError,
  resolveAccountScope,
  type AccountScopeRecord,
} from "../src/application/index.js";

const ACCOUNTS: readonly AccountScopeRecord[] = [
  { id: "account-b", legacyAccountKey: "XXX222", baseCurrency: "EUR", status: "active" },
  { id: "account-a", legacyAccountKey: "XXX111", baseCurrency: "USD", status: "active" },
  { id: "account-c", legacyAccountKey: "XXX333", baseCurrency: "USD", status: "archived" },
];

describe("resolveAccountScope", () => {
  it("all 只解析活跃账户并对异币种给出分组而非伪合计", () => {
    const resolved = resolveAccountScope({ kind: "all" }, ACCOUNTS);
    assert.deepEqual(resolved.accountIds, ["account-a", "account-b"]);
    assert.deepEqual(resolved.ledgerAccountKeys, ["XXX111", "XXX222"]);
    assert.deepEqual(resolved.currencyGroups, [
      { currency: "EUR", accountIds: ["account-b"] },
      { currency: "USD", accountIds: ["account-a"] },
    ]);
    assert.equal(resolved.aggregateCurrency, null);
  });

  it("允许显式查询归档账户，并拒绝缺省或不存在的账户范围", () => {
    const archived = resolveAccountScope(
      { kind: "account", account: "account-c" },
      ACCOUNTS,
    );
    assert.deepEqual(archived.ledgerAccountKeys, ["XXX333"]);
    assert.equal(archived.aggregateCurrency, "USD");

    for (const requestedScope of [
      undefined,
      { kind: "account", account: "missing" } as const,
    ]) {
      assert.throws(
        () => resolveAccountScope(requestedScope, ACCOUNTS),
        (error: unknown) => {
          assert.ok(error instanceof ApplicationError);
          assert.equal(
            error.code,
            requestedScope === undefined ? "ACCOUNT_SCOPE_REQUIRED" : "INVALID_ARGUMENT",
          );
          return true;
        },
      );
    }
  });
});
