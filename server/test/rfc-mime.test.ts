import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { describe, it } from "node:test";

import { parseEconfirm } from "../src/domain/gmail/index.js";
import { IngestError } from "../src/domain/ledger/index.js";
import { parseRfcMimeMessage } from "../src/infrastructure/gmail/index.js";

/** 构造可被既有 eConfirm 领域内核解析的股票成交正文。 */
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

/** 构造标准 multipart/alternative `.eml` 原文。 */
function multipartMessage(): Buffer {
  const plain = Buffer.from(tradeBody(), "utf8").toString("base64");
  const html = (
    "<html><body><div>Account ending: 276</div>"
    + "<div>Symbol:</div><div>INTC</div></body></html>"
  );
  return Buffer.from([
    "From: Schwab Alerts <donotreply@mail.schwab.com>",
    "Subject: Schwab eConfirms account ending in 276",
    "Message-ID: <original-eml-1@example.invalid>",
    "Date: Fri, 31 Jul 2026 16:25:59 +0000",
    "MIME-Version: 1.0",
    "Content-Type: multipart/alternative; boundary=lyra-boundary",
    "",
    "--lyra-boundary",
    "Content-Type: text/plain; charset=utf-8",
    "Content-Transfer-Encoding: base64",
    "",
    plain,
    "--lyra-boundary",
    "Content-Type: text/html; charset=utf-8",
    "",
    html,
    "--lyra-boundary--",
    "",
  ].join("\r\n"), "utf8");
}

describe("RFC MIME eConfirm adapter", () => {
  it("解码 multipart 正文并复用现有 eConfirm 金融规则", async () => {
    const message = await parseRfcMimeMessage({
      content: multipartMessage(),
      sourceName: "schwab.eml",
    });
    const [trade] = parseEconfirm(message);

    assert.equal(message.messageId, "original-eml-1@example.invalid");
    assert.equal(message.emailTimestamp, "2026-07-31T16:25:59.000Z");
    assert.equal(message.bodyCandidates?.length, 1);
    assert.equal(trade?.action, "Buy");
    assert.equal(trade?.price.toString(), "90.7154");
  });

  it("使用原文摘要补足消息 ID，并拒绝错误来源与附件式正文", async () => {
    const withoutMessageId = Buffer.from(
      multipartMessage().toString("utf8").replace(
        "Message-ID: <original-eml-1@example.invalid>\r\n",
        "",
      ),
      "utf8",
    );
    const message = await parseRfcMimeMessage({
      content: withoutMessageId,
      sourceName: "without-id.eml",
    });
    assert.equal(
      message.messageId,
      createHash("sha256").update(withoutMessageId).digest("hex"),
    );

    const wrongSender = Buffer.from(
      multipartMessage().toString("utf8").replace(
        "donotreply@mail.schwab.com",
        "alerts@mail.schwab.com",
      ),
      "utf8",
    );
    await assert.rejects(
      parseRfcMimeMessage({ content: wrongSender, sourceName: "wrong-sender.eml" }),
      (error: unknown) => error instanceof IngestError && error.message.includes("发件人"),
    );

    const attachmentOnly = Buffer.from([
      "From: Schwab Alerts <donotreply@mail.schwab.com>",
      "Subject: Schwab eConfirms account ending in 276",
      "MIME-Version: 1.0",
      "Content-Type: text/plain; charset=utf-8",
      "Content-Disposition: attachment; filename=confirm.txt",
      "",
      tradeBody(),
    ].join("\r\n"), "utf8");
    await assert.rejects(
      parseRfcMimeMessage({ content: attachmentOnly, sourceName: "attachment.eml" }),
      (error: unknown) => error instanceof IngestError && error.message.includes("内联"),
    );
  });
});
