import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createPublicGmailAccountMappingList } from "../src/application/index.js";
import type { PersistedGmailAccountMappingRecord } from "../src/application/index.js";

const MAPPINGS: readonly PersistedGmailAccountMappingRecord[] = [
  {
    id: "mapping-2",
    connectionId: "connection-1",
    accountSuffix: "902",
    accountId: "account-2",
    createdAt: "2026-08-31T16:00:00.000Z",
    updatedAt: "2026-08-31T16:00:00.000Z",
  },
  {
    id: "mapping-1",
    connectionId: "connection-1",
    accountSuffix: "276",
    accountId: "account-1",
    createdAt: "2026-08-31T15:00:00.000Z",
    updatedAt: "2026-08-31T15:00:00.000Z",
  },
];

describe("public Gmail account mapping list", () => {
  it("稳定公开脱敏映射并拒绝混入其他连接记录", () => {
    const result = createPublicGmailAccountMappingList("connection-1", MAPPINGS);

    assert.deepEqual(result, [
      {
        id: "mapping-1",
        accountSuffix: "276",
        accountId: "account-1",
        createdAt: "2026-08-31T15:00:00.000Z",
        updatedAt: "2026-08-31T15:00:00.000Z",
      },
      {
        id: "mapping-2",
        accountSuffix: "902",
        accountId: "account-2",
        createdAt: "2026-08-31T16:00:00.000Z",
        updatedAt: "2026-08-31T16:00:00.000Z",
      },
    ]);
    assert.equal(JSON.stringify(result).includes("connectionId"), false);
    assert.deepEqual(MAPPINGS.map((mapping) => mapping.id), ["mapping-2", "mapping-1"]);
    assert.throws(
      () => createPublicGmailAccountMappingList("connection-2", MAPPINGS),
      RangeError,
    );
  });
});
