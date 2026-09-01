import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  emailTradeToRawFields,
  extractAccountSuffix,
  parseEconfirm,
} from "../src/domain/gmail/index.js";
import type { GmailMessage } from "../src/domain/gmail/index.js";
import {
  IngestError,
  normalizeTransactionBatch,
} from "../src/domain/ledger/index.js";

/** 构造与 Gmail Markdown 正文相同结构的单笔成交块。 */
function tradeBlock(input: {
  symbol: string;
  description: string;
  action: string;
  positionType: string;
  quantity: string;
  price: string;
  principal: string;
  fees: string;
  amount: string;
}): string {
  const fee = Number(input.fees);
  const feeSection = fee === 0
    ? ""
    : `\n\n$${(fee - 0.01).toFixed(2)}\n\nIndustry Fee:\n\n$0.01\n\nTotal:\n\n$${fee.toFixed(2)}`;
  return (
    `\n\nSymbol:\n\n[${input.symbol}](https://example.invalid/security)`
    + `\n\nSecurity Description:\n\n${input.description}`
    + `\n\nAction:\n\n${input.action}`
    + "\n\nSecurity No./CUSIP:\n\n000000000000"
    + `\n\nType:\n\n${input.positionType}`
    + "\n\nTrade Date:\n\n07/23/26"
    + "\n\nSettle Date:\n\n07/24/26"
    + "\n\nQuantity\n\nPrice\n\nPrincipal\n\nCharge and/or Interest\n\nTotal Amount"
    + `\n\n${input.quantity}\n\n$${input.price}\n\n$${input.principal}`
    + `${feeSection}\n\n$${input.amount}`
    + "\n\nAdditional information for this security:"
  );
}

/** 构造已通过发件人层验证的标准正文邮件。 */
function message(body: string, candidates: readonly string[] = []): GmailMessage {
  return {
    messageId: "gmail-message-1",
    sender: "Schwab Alerts donotreply@mail.schwab.com",
    subject: "Schwab eConfirms account ending in 276",
    body: `Account ending: 276${body}`,
    emailTimestamp: "2026-07-24T16:25:59Z",
    bodyCandidates: candidates,
  };
}

describe("parseEconfirm actions", () => {
  it("组合期权 Action 与 Type 得到四种 Schwab 动作", () => {
    const cases = [
      ["Purchase", "Margin", "Buy to Open", "229.66", "-229.66"],
      ["Sale", "Margin", "Sell to Close", "228.34", "228.34"],
      ["Sale", "Short", "Sell to Open", "228.34", "228.34"],
      ["Purchase", "Short", "Buy to Close", "229.66", "-229.66"],
    ] as const;
    for (const [action, positionType, expectedAction, amount, expectedAmount] of cases) {
      const trade = parseEconfirm(message(tradeBlock({
        symbol: "INTC 07/31/2026 120.00 C",
        description: "INTEL CORP 07/31/2026 $120 Call",
        action,
        positionType,
        quantity: "1",
        price: "2.29",
        principal: "229.00",
        fees: "0.66",
        amount,
      })))[0];
      assert.equal(trade?.action, expectedAction);
      assert.equal(trade?.description, "CALL INTEL CORP $120 EXP 07/31/26");
      assert.equal(trade?.amount.toString(), expectedAmount);
    }
  });

  it("拒绝未知期权 Type", () => {
    assert.throws(
      () => parseEconfirm(message(tradeBlock({
        symbol: "INTC 07/31/2026 120.00 C",
        description: "INTEL CORP 07/31/2026 $120 Call",
        action: "Sale",
        positionType: "Unknown",
        quantity: "1",
        price: "2.29",
        principal: "229.00",
        fees: "0.66",
        amount: "228.34",
      }))),
      (error: unknown) => error instanceof IngestError && error.message.includes("无法映射 Action/Type"),
    );
  });
});

describe("parseEconfirm amount structures", () => {
  it("保留股票四位成交价并解析新版 N/A 零费用结构", () => {
    const body = tradeBlock({
      symbol: "INTC",
      description: "INTEL CORP",
      action: "Purchase",
      positionType: "Margin",
      quantity: "20",
      price: "104.129",
      principal: "2082.58",
      fees: "0",
      amount: "2082.58",
    }).replace(
      "\n\n$2082.58\n\nAdditional information",
      "\n\nN/A:\n\n$0.00\n\nN/A\n\n$2082.58\n\nFor the above:\n\nDisclosure\n\nAdditional information",
    );
    const trade = parseEconfirm(message(body))[0];
    assert.equal(trade?.price.toString(), "104.129");
    assert.equal(trade?.fees.toString(), "0");
    assert.equal(trade?.amount.toString(), "-2082.58");
  });

  it("解析仅 Industry Fee 的股票卖出并保留净额勾稽", () => {
    const base = tradeBlock({
      symbol: "INTC",
      description: "INTEL CORP",
      action: "Sale",
      positionType: "Margin",
      quantity: "10",
      price: "103.97",
      principal: "1039.70",
      fees: "0",
      amount: "1039.68",
    });
    const valid = base.replace(
      "\n\n$1039.68\n\nAdditional information",
      "\n\nIndustry Fee:\n\n$0.02\n\n$1,039.68\n\nAdditional information",
    );
    assert.equal(parseEconfirm(message(valid))[0]?.fees.toString(), "0.02");

    const invalidBuy = valid.replace("\n\nSale\n\n", "\n\nPurchase\n\n");
    assert.throws(
      () => parseEconfirm(message(invalidBuy)),
      (error: unknown) => error instanceof IngestError && error.message.includes("Total Amount 勾稽不符"),
    );
  });

  it("把零费用多价格股票成交展开为独立记录", () => {
    const body = (
      "\n\nSymbol:\n\nINTC"
      + "\n\nSecurity Description:\n\nINTEL CORP"
      + "\n\nAction:\n\nPurchase"
      + "\n\nSecurity No./CUSIP:\n\n458140100"
      + "\n\nType:\n\nMargin"
      + "\n\nTrade Date:\n\n07/01/26"
      + "\n\nSettle Date:\n\n07/02/26"
      + "\n\nQuantity\n\nPrice\n\nPrincipal\n\nCharge and/or Interest\n\nTotal Amount"
      + "\n\n10\n\n$134.655\n\n$1,346.55\n\n$1,346.55"
      + "\n\n10\n\n$130.30\n\n$1,303.00\n\n$1,303.00"
      + "\n\nTotals\n\n20\n\n$2,649.55\n\n$0.00\n\n$2,649.55"
      + "\n\nAdditional information for this security:"
    );
    const trades = parseEconfirm(message(body));
    assert.deepEqual(trades.map((trade) => trade.price.toString()), ["134.655", "130.3"]);
    assert.deepEqual(trades.map((trade) => trade.amount.toString()), ["-1346.55", "-1303"]);
  });
});

describe("eConfirm candidates and account", () => {
  it("拒绝 Subject 与正文账户尾号冲突", () => {
    assert.throws(
      () => extractAccountSuffix({ ...message(""), body: "Account ending: 222" }),
      (error: unknown) => error instanceof IngestError && error.message.includes("账户尾号不一致"),
    );
  });

  it("拒绝多个正文候选解析出不同成交", () => {
    const first = tradeBlock({
      symbol: "INTC", description: "INTEL CORP", action: "Purchase", positionType: "Margin",
      quantity: "1", price: "10.00", principal: "10.00", fees: "0", amount: "10.00",
    });
    const second = tradeBlock({
      symbol: "INTC", description: "INTEL CORP", action: "Purchase", positionType: "Margin",
      quantity: "1", price: "11.00", principal: "11.00", fees: "0", amount: "11.00",
    });
    assert.throws(
      () => parseEconfirm(message(first, [second])),
      (error: unknown) => error instanceof IngestError && error.message.includes("交易不一致"),
    );
  });
});

describe("eConfirm ledger bridge", () => {
  it("把邮件成交转换为八列并复用账本标准化与哈希", () => {
    const trade = parseEconfirm(message(tradeBlock({
      symbol: "INTC 07/31/2026 120.00 C",
      description: "INTEL CORP 07/31/2026 $120 Call",
      action: "Sale",
      positionType: "Short",
      quantity: "1",
      price: "2.29",
      principal: "229.00",
      fees: "0.66",
      amount: "228.34",
    })))[0];
    assert.notEqual(trade, undefined);
    if (trade === undefined) {
      throw new Error("测试邮件未生成成交");
    }

    const rawFields = emailTradeToRawFields(trade);
    const preview = normalizeTransactionBatch({
      fileName: "Individual_TST276_Transactions_GMAIL.csv",
      account: "TST276",
      sourceHash: "gmail-message-hash",
      sourceType: "gmail",
      sourceReference: "gmail-message-1",
      rows: [rawFields],
    });

    assert.equal(rawFields.Amount, "$228.34");
    assert.equal(preview.transactions[0]?.action, "Sell to Open");
    assert.equal(preview.transactions[0]?.assetType, "option");
    assert.equal(preview.transactions[0]?.fees?.toString(), "0.66");
  });
});
