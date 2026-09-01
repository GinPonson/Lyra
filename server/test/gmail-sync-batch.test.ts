import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  GmailSyncError,
  completeGmailSyncBatch,
} from "../src/domain/gmail/index.js";
import type { GmailSyncState } from "../src/domain/gmail/index.js";

/** 构造游标推进前的活跃 Gmail 连接状态。 */
function activeState(): GmailSyncState {
  return {
    status: "active",
    historyId: "100",
    lastSyncAt: "2026-08-30T00:00:00Z",
    lastErrorCode: null,
  };
}

describe("Gmail sync batch completion", () => {
  it("全部消息已有持久结果时汇总计数并推进 history 游标", () => {
    const completed = completeGmailSyncBatch({
      state: activeState(),
      expectedMessageIds: ["message-1", "message-2", "message-2", "message-3"],
      outcomes: [
        { messageId: "message-1", status: "imported" },
        { messageId: "message-2", status: "already_imported" },
        { messageId: "message-3", status: "rejected" },
      ],
      newHistoryId: "200",
      syncedAt: "2026-08-31T02:00:00Z",
    });

    assert.deepEqual(completed, {
      state: {
        status: "active",
        historyId: "200",
        lastSyncAt: "2026-08-31T02:00:00.000Z",
        lastErrorCode: null,
      },
      total: 3,
      imported: 1,
      alreadyImported: 1,
      rejected: 1,
      conflict: 0,
    });
  });

  it("消息失败、缺失或结果不属于本批次时不生成新游标状态", () => {
    const state = activeState();
    for (const outcomes of [
      [{ messageId: "message-1", status: "failed", errorCode: "TEMPORARY" }] as const,
      [] as const,
    ]) {
      assert.throws(
        () => completeGmailSyncBatch({
          state,
          expectedMessageIds: ["message-1"],
          outcomes,
          newHistoryId: "200",
          syncedAt: "2026-08-31T02:00:00Z",
        }),
        (error: unknown) => error instanceof GmailSyncError
          && error.code === "GMAIL_CONNECTION_ERROR",
      );
      assert.equal(state.historyId, "100");
    }
    assert.throws(
      () => completeGmailSyncBatch({
        state,
        expectedMessageIds: ["message-1"],
        outcomes: [{ messageId: "message-2", status: "imported" }],
        newHistoryId: "200",
        syncedAt: "2026-08-31T02:00:00Z",
      }),
      RangeError,
    );
  });
});
