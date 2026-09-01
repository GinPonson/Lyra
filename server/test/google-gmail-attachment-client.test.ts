import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { GmailSyncError } from "../src/domain/gmail/index.js";
import {
  buildSchwabEconfirmSearch,
  fetchBoundedGmailFullRecords,
  parseGmailFullInput,
} from "../src/infrastructure/gmail/index.js";

/** 构造不依赖真实 Gmail 网络的 JSON 响应。 */
function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** 构造正文由 attachmentId 承载的 Gmail full 响应。 */
function fullWithTextAttachment(): Record<string, unknown> {
  return {
    id: "message-1",
    internalDate: "1784900000000",
    payload: {
      mimeType: "multipart/alternative",
      headers: [
        { name: "From", value: "Schwab <donotreply@mail.schwab.com>" },
        { name: "Subject", value: "Schwab eConfirms account ending in 276" },
      ],
      body: {},
      parts: [{
        mimeType: "text/plain",
        headers: [{ name: "Content-Type", value: "text/plain; charset=UTF-8" }],
        body: { size: 40, attachmentId: "attachment-1" },
      }],
    },
  };
}

describe("Gmail text attachment hydration", () => {
  it("只补取 attachmentId 承载的文本正文并复用既有 full 解析器", async () => {
    const calls: URL[] = [];
    const body = "Account ending: 276\n\nSymbol:\n\nINTC";
    const fetched = await fetchBoundedGmailFullRecords(
      "access-secret",
      buildSchwabEconfirmSearch("2026-01-01", 1),
      async (input) => {
        const url = new URL(input.toString());
        calls.push(url);
        if (url.pathname.endsWith("/messages")) {
          return jsonResponse({ messages: [{ id: "message-1" }] });
        }
        if (url.pathname.endsWith("/attachments/attachment-1")) {
          return jsonResponse({ data: Buffer.from(body, "utf8").toString("base64url") });
        }
        return jsonResponse(fullWithTextAttachment());
      },
    );

    const [message] = parseGmailFullInput(fetched.records[0], "gmail-sync");
    assert.equal(message?.body, body);
    assert.equal(calls.some((url) => url.pathname.endsWith("/attachments/attachment-1")), true);
  });

  it("attachment API 缺少合法 data 时明确失败且不返回部分 full 记录", async () => {
    await assert.rejects(
      fetchBoundedGmailFullRecords(
        "access-secret",
        buildSchwabEconfirmSearch("2026-01-01", 1),
        async (input) => {
          const url = new URL(input.toString());
          if (url.pathname.endsWith("/messages")) {
            return jsonResponse({ messages: [{ id: "message-1" }] });
          }
          if (url.pathname.endsWith("/attachments/attachment-1")) {
            return jsonResponse({ size: 40 });
          }
          return jsonResponse(fullWithTextAttachment());
        },
      ),
      (error: unknown) => error instanceof GmailSyncError
        && error.code === "GMAIL_CONNECTION_ERROR",
    );
  });
});
