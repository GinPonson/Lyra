import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  GmailSyncError,
  completeGmailSync,
  markGmailHistoryExpired,
  planGmailSync,
} from "../src/domain/gmail/index.js";
import type {
  GmailSyncErrorCode,
  GmailSyncState,
} from "../src/domain/gmail/index.js";

/** 构造已经成功同步过的连接状态。 */
function activeState(): GmailSyncState {
  return {
    status: "active",
    historyId: "100",
    lastSyncAt: "2026-08-29T00:00:00Z",
    lastErrorCode: null,
  };
}

describe("Gmail sync state", () => {
  it("活跃连接只使用已持久化 history 游标增量同步", () => {
    assert.deepEqual(planGmailSync(activeState()), {
      mode: "incremental",
      startHistoryId: "100",
    });
  });

  it("首次扫描和游标失效都要求用户确认有界日期", () => {
    const pending: GmailSyncState = {
      status: "pending_initial_scan",
      historyId: null,
      lastSyncAt: null,
      lastErrorCode: null,
    };
    assert.throws(
      () => planGmailSync(pending),
      (error: unknown) => (
        error instanceof GmailSyncError
        && error.code === "GMAIL_RESYNC_CONFIRMATION_REQUIRED"
      ),
    );
    assert.deepEqual(planGmailSync(pending, "2026-01-01"), {
      mode: "bounded_rescan",
      afterDate: "2026-01-01",
    });
  });

  it("history 失效不自动重扫，成功后才推进游标并清除错误", () => {
    const active = activeState();
    const expired = markGmailHistoryExpired(active, "GMAIL_HISTORY_EXPIRED");

    assert.throws(
      () => markGmailHistoryExpired(active, "raw Google error" as GmailSyncErrorCode),
      RangeError,
    );

    assert.equal(expired.status, "needs_resync");
    assert.equal(expired.historyId, "100");
    assert.throws(
      () => planGmailSync(expired),
      (error: unknown) => (
        error instanceof GmailSyncError
        && error.code === "GMAIL_RESYNC_CONFIRMATION_REQUIRED"
      ),
    );

    const completed = completeGmailSync(
      expired,
      "200",
      "2026-08-30T00:00:00Z",
    );
    assert.deepEqual(completed, {
      status: "active",
      historyId: "200",
      lastSyncAt: "2026-08-30T00:00:00.000Z",
      lastErrorCode: null,
    });
    assert.equal(active.historyId, "100");
    assert.throws(
      () => completeGmailSync(active, "history-201", "2026-08-30T00:00:00Z"),
      RangeError,
    );
  });
});
