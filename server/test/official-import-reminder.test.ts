import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  DEFAULT_OFFICIAL_IMPORT_INTERVAL_MONTHS,
  planOfficialImportReminder,
} from "../src/application/index.js";

describe("official Schwab import reminder", () => {
  it("默认按自然月计划，并正确处理月末和到期当天", () => {
    const scheduled = planOfficialImportReminder({
      accountId: " account-1 ",
      enabled: true,
      lastSuccessfulImportDate: "2026-01-31",
      today: "2026-02-27",
    });
    const due = planOfficialImportReminder({
      accountId: "account-1",
      enabled: true,
      lastSuccessfulImportDate: "2026-01-31",
      today: "2026-02-28",
    });

    assert.deepEqual(scheduled, {
      accountId: "account-1",
      status: "scheduled",
      intervalMonths: DEFAULT_OFFICIAL_IMPORT_INTERVAL_MONTHS,
      dueDate: "2026-02-28",
    });
    assert.equal(due.status, "due");
    assert.equal(due.dueDate, "2026-02-28");
  });

  it("支持按账户关闭或调整周期，且从未导入时立即到期", () => {
    assert.deepEqual(planOfficialImportReminder({
      accountId: "account-2",
      enabled: false,
      intervalMonths: 3,
      lastSuccessfulImportDate: "2026-08-01",
      today: "2026-08-31",
    }), {
      accountId: "account-2",
      status: "disabled",
      intervalMonths: 3,
      dueDate: null,
    });
    assert.deepEqual(planOfficialImportReminder({
      accountId: "account-3",
      enabled: true,
      intervalMonths: 3,
      lastSuccessfulImportDate: null,
      today: "2026-08-31",
    }), {
      accountId: "account-3",
      status: "due",
      intervalMonths: 3,
      dueDate: "2026-08-31",
    });
  });
});
