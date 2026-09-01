import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { GmailSyncError } from "../src/domain/gmail/index.js";
import {
  GMAIL_PROFILE_ENDPOINT,
  buildSchwabEconfirmSearch,
  fetchInitialGmailFullRecords,
} from "../src/infrastructure/gmail/index.js";

/** 构造不依赖真实 Gmail 网络的 JSON 响应。 */
function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("Gmail initial scan REST client", () => {
  it("先固定 profile history 基线，再有界列出并获取 full 消息", async () => {
    const calls: URL[] = [];
    const result = await fetchInitialGmailFullRecords(
      "access-secret",
      "Trader@Example.com",
      buildSchwabEconfirmSearch("2026-01-01", 10),
      async (input) => {
        const url = new URL(input.toString());
        calls.push(url);
        if (url.pathname.endsWith("/profile")) {
          return jsonResponse({
            emailAddress: "trader@example.com",
            historyId: "2000",
            messagesTotal: 25,
            threadsTotal: 20,
          });
        }
        if (url.pathname.endsWith("/messages")) {
          return jsonResponse({ messages: [{ id: "message-1" }] });
        }
        return jsonResponse({ id: "message-1", payload: { mimeType: "text/plain" } });
      },
    );

    assert.equal(calls[0]?.toString(), GMAIL_PROFILE_ENDPOINT);
    assert.equal(calls[1]?.pathname.endsWith("/messages"), true);
    assert.deepEqual(result, {
      kind: "ready",
      records: [{ id: "message-1", payload: { mimeType: "text/plain" } }],
      completionHistoryId: "2000",
    });
  });

  it("首次扫描截断时不提供完成游标，并拒绝连接邮箱身份漂移", async () => {
    const profile = {
      emailAddress: "trader@example.com",
      historyId: "2000",
      messagesTotal: 25,
      threadsTotal: 20,
    };
    const truncated = await fetchInitialGmailFullRecords(
      "access-secret",
      "trader@example.com",
      buildSchwabEconfirmSearch("2026-01-01", 1),
      async (input) => {
        const url = new URL(input.toString());
        if (url.pathname.endsWith("/profile")) {
          return jsonResponse(profile);
        }
        if (url.pathname.endsWith("/messages")) {
          return jsonResponse({
            messages: [{ id: "message-1" }],
            nextPageToken: "page-2",
          });
        }
        return jsonResponse({ id: "message-1", payload: { mimeType: "text/plain" } });
      },
    );
    assert.equal(truncated.kind, "truncated");
    assert.equal("completionHistoryId" in truncated, false);

    await assert.rejects(
      fetchInitialGmailFullRecords(
        "access-secret",
        "other@example.com",
        buildSchwabEconfirmSearch("2026-01-01", 1),
        async () => jsonResponse(profile),
      ),
      (error: unknown) => error instanceof GmailSyncError
        && error.code === "GMAIL_CONNECTION_ERROR",
    );
  });
});
