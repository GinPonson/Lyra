import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { GmailMessage } from "../src/domain/gmail/index.js";
import {
  createUnsuccessfulGmailMessageMetadata,
} from "../src/infrastructure/gmail/index.js";

/** 构造无法进入交易账本但仍需摘要归档的 Gmail 消息。 */
function message(body: string): GmailMessage {
  return {
    messageId: "message-failed-1",
    sender: "Schwab <donotreply@mail.schwab.com>",
    subject: "Schwab eConfirms account ending in 276",
    body,
    emailTimestamp: "1788195600000",
  };
}

/** 构造失败元数据函数的固定非正文上下文。 */
function input(
  body: string,
  errorCode: "GMAIL_MESSAGE_REJECTED" | "ACCOUNT_MAPPING_REQUIRED" | "GMAIL_MESSAGE_CONFLICT",
) {
  return {
    connectionId: "connection-1",
    threadId: null,
    message: message(body),
    accountId: null,
    historyId: "300",
    syncedAt: new Date("2026-08-31T03:00:00Z"),
    errorCode,
  };
}

describe("unsuccessful Gmail message metadata", () => {
  it("解析拒绝只保存稳定错误码和规范内容摘要，不保存正文或失败详情", () => {
    const unix = createUnsuccessfulGmailMessageMetadata(input(
      "Account ending: 276\nunknown format",
      "GMAIL_MESSAGE_REJECTED",
    ));
    const windows = createUnsuccessfulGmailMessageMetadata(input(
      "Account ending: 276\r\nunknown format",
      "GMAIL_MESSAGE_REJECTED",
    ));

    assert.equal(unix.contentHash, windows.contentHash);
    assert.equal(unix.parseStatus, "rejected");
    assert.equal(unix.errorCode, "GMAIL_MESSAGE_REJECTED");
    assert.equal(unix.transactionCount, 0);
    assert.equal("body" in unix, false);
    assert.equal("errorMessage" in unix, false);
  });

  it("账户映射和同 ID 变化归档为 conflict 而不是 rejected", () => {
    for (const errorCode of ["ACCOUNT_MAPPING_REQUIRED", "GMAIL_MESSAGE_CONFLICT"] as const) {
      const metadata = createUnsuccessfulGmailMessageMetadata(input("body", errorCode));
      assert.equal(metadata.parseStatus, "conflict");
      assert.equal(metadata.errorCode, errorCode);
    }
  });
});
