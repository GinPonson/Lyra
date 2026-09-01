import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { parseEconfirm } from "../src/domain/gmail/index.js";
import { IngestError } from "../src/domain/ledger/index.js";
import { parseGmailFullInput } from "../src/infrastructure/gmail/index.js";

/** 构造可被 eConfirm 领域解析器接受的单笔股票成交正文。 */
function tradeBody(): string {
  return (
    "Account ending: 276"
    + "\n\nSymbol:\n\nINTC"
    + "\n\nSecurity Description:\n\nINTEL CORP"
    + "\n\nAction:\n\nPurchase"
    + "\n\nSecurity No./CUSIP:\n\n458140100"
    + "\n\nType:\n\nMargin"
    + "\n\nTrade Date:\n\n07/31/26"
    + "\n\nSettle Date:\n\n08/03/26"
    + "\n\nQuantity\n\nPrice\n\nPrincipal\n\nCharge and/or Interest"
    + "\n\nTotal Amount\n\n10\n\n$90.7154\n\n$907.15"
    + "\n\nN/A:\n\n$0.00\n\nN/A\n\n$907.15"
    + "\n\nFor the above:\n\nSynthetic disclosure"
    + "\n\nAdditional information for this security:"
  );
}

/** 把 UTF-8 正文编码为 Gmail body.data 使用的 Base64URL。 */
function base64Url(value: string): string {
  return Buffer.from(value, "utf8").toString("base64url");
}

/** 构造一个标准 users.messages.get(format=full) 响应。 */
function fullRecord(body: string, mimeType = "text/plain"): Record<string, unknown> {
  return {
    id: "gmail-full-1",
    internalDate: "1784900000000",
    payload: {
      mimeType,
      headers: [
        { name: "From", value: "Schwab Alerts <donotreply@mail.schwab.com>" },
        { name: "Subject", value: "Schwab eConfirms account ending in 276" },
        { name: "Date", value: "Fri, 31 Jul 2026 16:25:59 +0000" },
        { name: "Content-Type", value: `${mimeType}; charset=UTF-8` },
      ],
      body: { size: body.length, data: base64Url(body) },
    },
  };
}

describe("parseGmailFullInput", () => {
  it("解析标准 full 响应并直接进入 eConfirm 领域内核", () => {
    const [message] = parseGmailFullInput(fullRecord(tradeBody()), "gmail-full.json");
    assert.notEqual(message, undefined);
    if (message === undefined) {
      throw new Error("测试 full 响应未生成邮件");
    }

    const [trade] = parseEconfirm(message);
    assert.equal(message.messageId, "gmail-full-1");
    assert.equal(message.emailTimestamp, "Fri, 31 Jul 2026 16:25:59 +0000");
    assert.equal(trade?.action, "Buy");
    assert.equal(trade?.price.toString(), "90.7154");
  });

  it("在没有 plain part 时提取 HTML 可见文本边界", () => {
    const html = (
      "<html><body><style>.hidden{display:none}</style>"
      + "<div>Account ending: 276</div><div>Symbol:</div><div>INTC</div>"
      + "</body></html>"
    );
    const [message] = parseGmailFullInput(fullRecord(html, "text/html"), "gmail-html.json");

    assert.equal(message?.body.includes("Account ending: 276"), true);
    assert.equal(message?.body.includes("Symbol:\n\nINTC"), true);
    assert.equal(message?.body.includes("display:none"), false);
  });

  it("拒绝 list 摘要、非 Schwab 发件人和附件式正文", () => {
    assert.throws(
      () => parseGmailFullInput({ messages: [{ id: "m1" }] }, "gmail-list.json"),
      (error: unknown) => error instanceof IngestError && error.message.includes("messages.get"),
    );

    const wrongSender = fullRecord(tradeBody());
    const payload = wrongSender.payload as { headers: { name: string; value: string }[] };
    payload.headers[0] = { name: "From", value: "Schwab Alerts <alerts@mail.schwab.com>" };
    assert.throws(
      () => parseGmailFullInput(wrongSender, "wrong-sender.json"),
      (error: unknown) => error instanceof IngestError && error.message.includes("发件人"),
    );

    const attachmentOnly = fullRecord(tradeBody());
    const attachmentPayload = attachmentOnly.payload as { body: Record<string, unknown> };
    attachmentPayload.body = { size: 100, attachmentId: "attachment-1" };
    assert.throws(
      () => parseGmailFullInput(attachmentOnly, "attachment.json"),
      (error: unknown) => error instanceof IngestError && error.message.includes("attachment data"),
    );
  });
});
