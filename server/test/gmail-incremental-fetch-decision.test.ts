import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  GmailSyncError,
  decideGmailIncrementalFetch,
  planGmailSync,
} from "../src/domain/gmail/index.js";
import type { GmailSyncState } from "../src/domain/gmail/index.js";

/** 构造具有持久化 history 游标的活跃连接。 */
function activeState(): GmailSyncState {
  return {
    status: "active",
    historyId: "1000",
    lastSyncAt: "2026-08-31T00:00:00.000Z",
    lastErrorCode: null,
  };
}

describe("Gmail incremental fetch decision", () => {
  it("完整增量只交出候选 completion history id，不提前修改连接状态", () => {
    const state = activeState();
    const decision = decideGmailIncrementalFetch(state, {
      observedHistoryId: "1003",
      candidateMessageCount: 2,
      truncated: false,
    });

    assert.deepEqual(decision, {
      kind: "ready",
      completionHistoryId: "1003",
      candidateMessageCount: 2,
    });
    assert.deepEqual(state, activeState());
  });

  it("截断时保留旧游标并强制进入需确认重扫，不暴露可误用的新游标", () => {
    const decision = decideGmailIncrementalFetch(activeState(), {
      observedHistoryId: "2000",
      candidateMessageCount: 500,
      truncated: true,
    });

    assert.equal(decision.kind, "needs_resync");
    if (decision.kind !== "needs_resync") {
      throw new Error("截断增量未进入 needs_resync");
    }
    assert.equal("completionHistoryId" in decision, false);
    assert.equal(decision.state.historyId, "1000");
    assert.equal(decision.state.status, "needs_resync");
    assert.equal(decision.state.lastErrorCode, "GMAIL_INCREMENTAL_LIMIT_EXCEEDED");
    assert.throws(
      () => planGmailSync(decision.state),
      (error: unknown) => error instanceof GmailSyncError
        && error.code === "GMAIL_RESYNC_CONFIRMATION_REQUIRED",
    );
    assert.deepEqual(planGmailSync(decision.state, "2026-08-01"), {
      mode: "bounded_rescan",
      afterDate: "2026-08-01",
    });
  });
});
