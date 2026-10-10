import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  ApplicationError,
  resolveImportAccount,
  type ImportAccountRecord,
} from "../src/application/index.js";

const ACCOUNTS: readonly ImportAccountRecord[] = [
  { id: "active-a", legacyAccountKey: "IRA276", maskedSuffix: "276", status: "active" },
  { id: "active-b", legacyAccountKey: "TST276", maskedSuffix: "276", status: "active" },
  { id: "archived", legacyAccountKey: "OLD999", maskedSuffix: "999", status: "archived" },
];

describe("resolveImportAccount", () => {
  it("仅对唯一完整旧账户键自动匹配", () => {
    assert.deepEqual(resolveImportAccount("TST276", ACCOUNTS, null), {
      accountId: "active-b",
      legacyAccountKey: "TST276",
      matchedBy: "legacy_key",
    });
    assert.throws(
      () => resolveImportAccount("NEW276", ACCOUNTS, null),
      (error: unknown) => {
        assert.ok(error instanceof ApplicationError);
        assert.equal(error.code, "ACCOUNT_MAPPING_REQUIRED");
        assert.deepEqual(error.details, {
          candidate_account_ids: ["active-a", "active-b"],
        });
        return true;
      },
    );
  });

  it("显式选择仍校验线索且不能选择归档账户", () => {
    assert.deepEqual(resolveImportAccount("NEW276", ACCOUNTS, "active-a"), {
      accountId: "active-a",
      legacyAccountKey: "IRA276",
      matchedBy: "explicit_selection",
    });
    for (const accountId of ["active-a", "archived"]) {
      assert.throws(
        () => resolveImportAccount("NEW999", ACCOUNTS, accountId),
        (error: unknown) => (
          error instanceof ApplicationError
          && error.code === "ACCOUNT_MAPPING_REQUIRED"
        ),
      );
    }
  });

  it("大小写匹配沿用持久化的旧键，保持已有交易哈希与账户归属", () => {
    const account = { id: "legacy-lowercase", legacyAccountKey: "xxx276", maskedSuffix: "276", status: "active" as const };
    assert.deepEqual(resolveImportAccount(" XXX276 ", [account], null), {
      accountId: account.id,
      legacyAccountKey: "xxx276",
      matchedBy: "legacy_key",
    });
    assert.deepEqual(resolveImportAccount("nEw276", [account], account.id), {
      accountId: account.id,
      legacyAccountKey: "xxx276",
      matchedBy: "explicit_selection",
    });
    assert.throws(
      () => resolveImportAccount("XXX276", [account, { ...account, id: "second", legacyAccountKey: "XXX276" }], null),
      (error: unknown) => error instanceof ApplicationError && error.code === "ACCOUNT_MAPPING_REQUIRED",
    );
  });
});
