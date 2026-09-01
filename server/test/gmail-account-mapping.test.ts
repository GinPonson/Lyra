import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ApplicationError,
  planGmailAccountMappingUpdate,
  resolveGmailAccountMapping,
} from "../src/application/index.js";
import type { ImportAccountRecord } from "../src/application/index.js";

/** 构造 Gmail 映射测试使用的账户最小投影。 */
function accounts(): readonly ImportAccountRecord[] {
  return [
    { id: "account-1", legacyAccountKey: "IRA276", maskedSuffix: "276", status: "active" },
    { id: "account-2", legacyAccountKey: "TST276", maskedSuffix: "276", status: "active" },
    { id: "account-3", legacyAccountKey: "OLD999", maskedSuffix: "999", status: "archived" },
  ];
}

describe("Gmail account mapping", () => {
  it("即使尾号候选唯一也要求首次显式选择并生成待持久化映射", () => {
    assert.throws(
      () => resolveGmailAccountMapping({
        connectionId: "connection-1",
        accountSuffix: "276",
        accounts: [accounts()[0]!],
        existingMapping: null,
        explicitAccountId: null,
      }),
      (error: unknown) => {
        assert.ok(error instanceof ApplicationError);
        assert.equal(error.code, "ACCOUNT_MAPPING_REQUIRED");
        assert.deepEqual(error.details?.candidate_account_ids, ["account-1"]);
        return true;
      },
    );

    const resolved = resolveGmailAccountMapping({
      connectionId: " connection-1 ",
      accountSuffix: "276",
      accounts: accounts(),
      existingMapping: null,
      explicitAccountId: "account-2",
    });
    assert.deepEqual(resolved, {
      accountId: "account-2",
      legacyAccountKey: "TST276",
      matchedBy: "explicit_selection",
      mappingToCreate: {
        connectionId: "connection-1",
        accountSuffix: "276",
        accountId: "account-2",
      },
    });
  });

  it("复用已有活跃映射，并拒绝同步时改绑或导入归档账户", () => {
    const existingMapping = {
      connectionId: "connection-1",
      accountSuffix: "276",
      accountId: "account-1",
    };
    const resolved = resolveGmailAccountMapping({
      connectionId: "connection-1",
      accountSuffix: "276",
      accounts: accounts(),
      existingMapping,
      explicitAccountId: null,
    });
    assert.equal(resolved.matchedBy, "existing_mapping");
    assert.equal(resolved.mappingToCreate, null);

    assert.throws(
      () => resolveGmailAccountMapping({
        connectionId: "connection-1",
        accountSuffix: "276",
        accounts: accounts(),
        existingMapping,
        explicitAccountId: "account-2",
      }),
      (error: unknown) => error instanceof ApplicationError
        && error.code === "ACCOUNT_MAPPING_REQUIRED",
    );
    assert.throws(
      () => resolveGmailAccountMapping({
        connectionId: "connection-1",
        accountSuffix: "999",
        accounts: accounts(),
        existingMapping: null,
        explicitAccountId: "account-3",
      }),
      (error: unknown) => error instanceof ApplicationError
        && error.code === "ACCOUNT_MAPPING_REQUIRED",
    );
  });

  it("为用户显式改绑生成 CAS 计划且相同目标幂等", () => {
    const current = {
      connectionId: "connection-1",
      accountSuffix: "276",
      accountId: "account-1",
    };
    const changed = planGmailAccountMappingUpdate({
      connectionId: "connection-1",
      mapping: current,
      accounts: accounts(),
      targetAccountId: "account-2",
    });
    assert.deepEqual(changed, {
      expectedAccountId: "account-1",
      nextMapping: {
        connectionId: "connection-1",
        accountSuffix: "276",
        accountId: "account-2",
      },
      changed: true,
    });
    assert.deepEqual(current, {
      connectionId: "connection-1",
      accountSuffix: "276",
      accountId: "account-1",
    });

    const unchanged = planGmailAccountMappingUpdate({
      connectionId: "connection-1",
      mapping: current,
      accounts: accounts(),
      targetAccountId: "account-1",
    });
    assert.equal(unchanged.changed, false);
    assert.equal(unchanged.nextMapping, current);
  });

  it("拒绝跨连接、尾号不符和归档账户的显式改绑", () => {
    const mapping = {
      connectionId: "connection-1",
      accountSuffix: "276",
      accountId: "account-1",
    };
    assert.throws(
      () => planGmailAccountMappingUpdate({
        connectionId: "connection-2",
        mapping,
        accounts: accounts(),
        targetAccountId: "account-2",
      }),
      (error: unknown) => error instanceof ApplicationError
        && error.code === "INVALID_ARGUMENT",
    );
    for (const targetAccountId of ["account-3", "missing-account"]) {
      assert.throws(
        () => planGmailAccountMappingUpdate({
          connectionId: "connection-1",
          mapping,
          accounts: accounts(),
          targetAccountId,
        }),
        (error: unknown) => error instanceof ApplicationError
          && error.code === "ACCOUNT_MAPPING_REQUIRED",
      );
    }
  });
});
