import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { GmailMessage } from "../src/domain/gmail/index.js";
import { IngestError } from "../src/domain/ledger/index.js";
import { createParsedGmailMessageMetadata } from "../src/infrastructure/gmail/index.js";

/** 构造元数据测试使用的最小已验证邮件。 */
function message(emailTimestamp: string | null, body = "Account ending: 276\nSymbol: INTC"): GmailMessage {
  return {
    messageId: "message-1",
    sender: "Schwab <donotreply@mail.schwab.com>",
    subject: "Schwab eConfirms",
    body,
    emailTimestamp,
  };
}

describe("parsed Gmail message metadata", () => {
  it("生成稳定内容摘要和 UTC 时间且不持久化正文", () => {
    const create = (body: string) => createParsedGmailMessageMetadata({
      connectionId: "connection-1",
      threadId: "thread-1",
      message: message("Fri, 31 Jul 2026 16:25:59 +0000", body),
      trades: [],
      accountId: null,
      historyId: "1",
      syncedAt: new Date("2026-08-31T00:00:00Z"),
    });
    const unixNewlines = create("Account ending: 276\nSymbol: INTC");
    const windowsNewlines = create("Account ending: 276\r\nSymbol: INTC");

    assert.equal(unixNewlines.contentHash, windowsNewlines.contentHash);
    assert.equal(unixNewlines.receivedAt, "2026-07-31T16:25:59.000Z");
    assert.equal(unixNewlines.parseStatus, "parsed");
    assert.equal(unixNewlines.transactionCount, 0);
    assert.equal(unixNewlines.historyId, "1");
    assert.equal("body" in unixNewlines, false);
  });

  it("接受 Gmail internalDate 毫秒值并明确拒绝非法时间", () => {
    const metadata = createParsedGmailMessageMetadata({
      connectionId: "connection-1",
      threadId: null,
      message: message("1784900000000"),
      trades: [],
      accountId: "account-1",
      historyId: null,
      syncedAt: new Date("2026-08-31T00:00:00Z"),
    });
    assert.equal(metadata.receivedAt, new Date(1_784_900_000_000).toISOString());
    assert.throws(
      () => createParsedGmailMessageMetadata({
        connectionId: "connection-1",
        threadId: null,
        message: message("not-a-timestamp"),
        trades: [],
        accountId: null,
        historyId: null,
        syncedAt: new Date("2026-08-31T00:00:00Z"),
      }),
      IngestError,
    );
  });
});
