import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { GmailSyncError } from "../src/domain/gmail/index.js";
import {
  GMAIL_MESSAGES_ENDPOINT,
  buildSchwabEconfirmSearch,
  fetchBoundedGmailFullRecords,
} from "../src/infrastructure/gmail/index.js";
import { serializeApiError } from "../src/transport/index.js";

/** 构造不依赖真实 Gmail 网络的 JSON 响应。 */
function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("Gmail bounded REST client", () => {
  it("在任务硬上限内分页列出 id 并逐封取得 format=full", async () => {
    const calls: Array<{ url: URL; init: RequestInit | undefined }> = [];
    const fetchFn = async (input: string | URL, init?: RequestInit): Promise<Response> => {
      const url = new URL(input.toString());
      calls.push({ url, init });
      if (url.pathname.endsWith("/messages")) {
        if (url.searchParams.get("pageToken") === null) {
          return jsonResponse({
            messages: [{ id: "message-1" }, { id: "message-2" }],
            nextPageToken: "page-2",
          });
        }
        return jsonResponse({
          messages: [{ id: "message-3" }],
          nextPageToken: "page-3",
        });
      }
      const messageId = url.pathname.split("/").at(-1);
      return jsonResponse({ id: messageId, payload: { mimeType: "text/plain" } });
    };

    const result = await fetchBoundedGmailFullRecords(
      "access-secret",
      buildSchwabEconfirmSearch("2026-01-01", 3),
      fetchFn,
    );

    assert.equal(calls.length, 5);
    const firstCall = calls[0];
    assert.notEqual(firstCall, undefined);
    if (firstCall === undefined) {
      throw new Error("测试未捕获 Gmail messages.list 请求");
    }
    assert.equal(firstCall.url.origin + firstCall.url.pathname, GMAIL_MESSAGES_ENDPOINT);
    assert.equal(firstCall.url.searchParams.get("maxResults"), "3");
    assert.equal(calls[1]?.url.searchParams.get("maxResults"), "1");
    assert.equal(calls[1]?.url.searchParams.get("pageToken"), "page-2");
    assert.equal(calls.every((call) => (
      (call.init?.headers as Record<string, string>).authorization === "Bearer access-secret"
    )), true);
    assert.equal(calls.slice(2).every((call) => call.url.searchParams.get("format") === "full"), true);
    assert.deepEqual(result.records.map((record) => record.id), [
      "message-1",
      "message-2",
      "message-3",
    ]);
    assert.equal(result.truncated, true);
  });

  it("把授权失效与临时上游故障映射为脱敏稳定错误", async () => {
    const plan = buildSchwabEconfirmSearch("2026-01-01", 10);
    await assert.rejects(
      fetchBoundedGmailFullRecords(
        "access-secret",
        plan,
        async () => jsonResponse({ error: "invalid_token", secret: "raw-secret" }, 401),
      ),
      (error: unknown) => error instanceof GmailSyncError
        && error.code === "GMAIL_AUTH_REVOKED"
        && error.retryable === false,
    );

    let unavailable: unknown;
    try {
      await fetchBoundedGmailFullRecords(
        "access-secret",
        plan,
        async () => jsonResponse({ error: "backendError", secret: "raw-secret" }, 503),
      );
    } catch (error) {
      unavailable = error;
    }
    assert.ok(unavailable instanceof GmailSyncError);
    const response = serializeApiError(unavailable, "request-gmail");
    assert.equal(response.statusCode, 503);
    assert.equal(response.body.code, "GMAIL_CONNECTION_ERROR");
    assert.equal(response.body.retryable, true);
    assert.equal(JSON.stringify(response).includes("raw-secret"), false);
    assert.equal(JSON.stringify(response).includes("access-secret"), false);
  });
});
