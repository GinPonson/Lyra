import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { GmailSyncError } from "../src/domain/gmail/index.js";
import {
  GMAIL_HISTORY_ENDPOINT,
  fetchIncrementalGmailFullRecords,
} from "../src/infrastructure/gmail/index.js";

/** 构造不依赖真实 Gmail 网络的 JSON 响应。 */
function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("Gmail history REST client", () => {
  it("只读取 messageAdded、去重 message id 并返回可推进的 observed history id", async () => {
    const calls: URL[] = [];
    const result = await fetchIncrementalGmailFullRecords(
      "access-secret",
      { startHistoryId: "1000", maximumMessages: 10 },
      async (input) => {
        const url = new URL(input.toString());
        calls.push(url);
        if (url.pathname.endsWith("/history")) {
          return jsonResponse({
            history: [
              {
                id: "1001",
                messagesAdded: [
                  { message: { id: "message-1" } },
                  { message: { id: "message-2" } },
                ],
              },
              {
                id: "1002",
                messagesAdded: [{ message: { id: "message-1" } }],
              },
            ],
            historyId: "1003",
          });
        }
        const messageId = url.pathname.split("/").at(-1);
        if (url.searchParams.get("format") === "metadata") {
          return jsonResponse({
            id: messageId,
            payload: {
              headers: messageId === "message-1"
                ? [
                    { name: "From", value: "Schwab <donotreply@mail.schwab.com>" },
                    { name: "Subject", value: "Schwab eConfirms account ending in 276" },
                  ]
                : [
                    { name: "From", value: "newsletter@example.com" },
                    { name: "Subject", value: "Weekly report" },
                  ],
            },
          });
        }
        return jsonResponse({ id: messageId, payload: { mimeType: "text/plain" } });
      },
    );

    const historyCall = calls[0];
    assert.notEqual(historyCall, undefined);
    if (historyCall === undefined) {
      throw new Error("测试未捕获 Gmail history.list 请求");
    }
    assert.equal(historyCall.origin + historyCall.pathname, GMAIL_HISTORY_ENDPOINT);
    assert.equal(historyCall.searchParams.get("startHistoryId"), "1000");
    assert.equal(historyCall.searchParams.get("historyTypes"), "messageAdded");
    assert.deepEqual(result.records.map((record) => record.id), ["message-1"]);
    assert.equal(result.ignoredMessageCount, 1);
    assert.equal(result.observedHistoryId, "1003");
    assert.equal(result.truncated, false);
  });

  it("达到增量硬上限时禁止静默完成，并把 404 映射为游标过期", async () => {
    const truncated = await fetchIncrementalGmailFullRecords(
      "access-secret",
      { startHistoryId: "1000", maximumMessages: 1 },
      async (input) => {
        const url = new URL(input.toString());
        if (url.pathname.endsWith("/history")) {
          return jsonResponse({
            history: [{
              id: "1001",
              messagesAdded: [
                { message: { id: "message-1" } },
                { message: { id: "message-2" } },
              ],
            }],
            nextPageToken: "page-2",
            historyId: "1002",
          });
        }
        if (url.searchParams.get("format") === "metadata") {
          return jsonResponse({
            id: "message-1",
            payload: {
              headers: [
                { name: "From", value: "Schwab <donotreply@mail.schwab.com>" },
                { name: "Subject", value: "Schwab eConfirms account ending in 276" },
              ],
            },
          });
        }
        return jsonResponse({ id: "message-1", payload: { mimeType: "text/plain" } });
      },
    );
    assert.equal(truncated.records.length, 1);
    assert.equal(truncated.truncated, true);

    await assert.rejects(
      fetchIncrementalGmailFullRecords(
        "access-secret",
        { startHistoryId: "1000", maximumMessages: 10 },
        async () => jsonResponse({ error: "notFound", secret: "raw-secret" }, 404),
      ),
      (error: unknown) => error instanceof GmailSyncError
        && error.code === "GMAIL_HISTORY_EXPIRED"
        && !error.message.includes("raw-secret"),
    );
  });
});
