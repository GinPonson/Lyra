import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  createLedgerReadResult,
  resolveAccountScope,
  type AccountScopeRecord,
} from "../src/application/index.js";

const ACCOUNTS: readonly AccountScopeRecord[] = [
  { id: "account-usd", legacyAccountKey: "USD001", baseCurrency: "USD", status: "active" },
  { id: "account-eur", legacyAccountKey: "EUR001", baseCurrency: "EUR", status: "active" },
];

describe("ledger read result", () => {
  it("固定账户、币种和账本基线，并把时间规范为 UTC", () => {
    const scope = resolveAccountScope({ kind: "all" }, ACCOUNTS);
    const data = { totalRealizedPnl: "12.3400" };
    const result = createLedgerReadResult({
      data,
      scope,
      ledgerVersion: 42,
      asOf: "2026-08-31T20:00:00+08:00",
      fetchedAt: new Date("2026-08-31T12:00:01.250Z"),
    });

    assert.equal(result.data, data);
    assert.deepEqual(result.metadata, {
      accountScope: { kind: "all" },
      accountIds: ["account-eur", "account-usd"],
      currencyGroups: [
        { currency: "EUR", accountIds: ["account-eur"] },
        { currency: "USD", accountIds: ["account-usd"] },
      ],
      aggregateCurrency: null,
      ledgerVersion: 42,
      asOf: "2026-08-31T12:00:00.000Z",
      fetchedAt: "2026-08-31T12:00:01.250Z",
    });
  });

  it("拒绝非法账本版本、无时区截止时间和无效服务端时钟", () => {
    const scope = resolveAccountScope(
      { kind: "account", account: "account-usd" },
      ACCOUNTS,
    );
    const base = {
      data: {},
      scope,
      ledgerVersion: 1,
      asOf: "2026-08-31T12:00:00Z",
      fetchedAt: new Date("2026-08-31T12:00:01Z"),
    };

    assert.throws(
      () => createLedgerReadResult({ ...base, ledgerVersion: -1 }),
      RangeError,
    );
    assert.throws(
      () => createLedgerReadResult({ ...base, asOf: "2026-08-31T12:00:00" }),
      RangeError,
    );
    assert.throws(
      () => createLedgerReadResult({ ...base, fetchedAt: new Date("invalid") }),
      RangeError,
    );
  });
});
