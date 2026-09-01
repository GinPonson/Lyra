import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  planScheduledGmailSync,
} from "../src/domain/gmail/index.js";
import type { GmailSyncState } from "../src/domain/gmail/index.js";

/** 构造已有一次成功同步的 active 连接。 */
function activeState(): GmailSyncState {
  return {
    status: "active",
    historyId: "90071992547409931234",
    lastSyncAt: "2026-08-31T01:00:00Z",
    lastErrorCode: null,
  };
}

describe("scheduled Gmail sync", () => {
  it("默认一小时到期后只生成增量请求，并保留超大 history ID 字符串", () => {
    const plan = planScheduledGmailSync({
      state: activeState(),
      now: new Date("2026-08-31T02:00:00Z"),
      syncInProgress: false,
    });

    assert.deepEqual(plan, {
      kind: "start",
      dueAt: "2026-08-31T02:00:00.000Z",
      request: {
        mode: "incremental",
        startHistoryId: "90071992547409931234",
      },
    });
  });

  it("未到期、已有任务或等待确认重扫时明确跳过", () => {
    assert.deepEqual(planScheduledGmailSync({
      state: activeState(),
      now: new Date("2026-08-31T01:30:00Z"),
      syncInProgress: false,
    }), {
      kind: "skip",
      reason: "NOT_DUE",
      nextDueAt: "2026-08-31T02:00:00.000Z",
    });
    assert.deepEqual(planScheduledGmailSync({
      state: activeState(),
      now: new Date("2026-08-31T03:00:00Z"),
      syncInProgress: true,
    }), {
      kind: "skip",
      reason: "SYNC_IN_PROGRESS",
      nextDueAt: null,
    });
    assert.deepEqual(planScheduledGmailSync({
      state: { ...activeState(), status: "needs_resync" },
      now: new Date("2026-08-31T03:00:00Z"),
      syncInProgress: false,
    }), {
      kind: "skip",
      reason: "CONNECTION_NOT_ACTIVE",
      nextDueAt: null,
    });
  });
});
