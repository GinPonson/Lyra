import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { Decimal } from "decimal.js";

import { ApplicationError } from "../src/application/index.js";
import type {
  EmailTrade,
  GmailMessage,
} from "../src/domain/gmail/index.js";
import {
  buildGmailMessageImportPlan,
} from "../src/infrastructure/gmail/index.js";

/** 构造已由 Gmail MIME 层验证并提取正文的标准消息。 */
function message(body = "Account ending: 276\nSymbol: INTC"): GmailMessage {
  return {
    messageId: "message-1",
    sender: "Schwab <donotreply@mail.schwab.com>",
    subject: "Schwab eConfirms account ending in 276",
    body,
    emailTimestamp: "2026-07-01T18:00:00Z",
  };
}

/** 构造已通过 eConfirm 金额勾稽的股票买入成交。 */
function trade(): EmailTrade {
  return {
    tradeDate: "2026-07-01",
    action: "Buy",
    symbol: "INTC",
    description: "INTEL CORP",
    quantity: new Decimal("1"),
    price: new Decimal("10"),
    fees: new Decimal("0"),
    amount: new Decimal("-10"),
  };
}

/** 提供构建消息导入计划所需的固定同步上下文。 */
function input(existingMessage: Parameters<typeof buildGmailMessageImportPlan>[0]["existingMessage"] = null) {
  return {
    connectionId: "connection-1",
    threadId: "thread-1",
    message: message(),
    trades: [trade()],
    accountId: "account-1",
    ledgerAccount: "TST276",
    historyId: "2",
    syncedAt: new Date("2026-08-31T01:00:00Z"),
    parserVersion: "econfirm-1.0.0",
    baseLedgerVersion: 3,
    existingMessage,
    existingAccountTransactions: [],
  };
}

describe("Gmail message import plan", () => {
  it("复用标准交易与 FIFO 内核生成无正文的原子写入计划", () => {
    const plan = buildGmailMessageImportPlan(input());

    assert.equal(plan.kind, "ready");
    if (plan.kind !== "ready") {
      throw new Error("测试消息未生成 ready 导入计划");
    }
    assert.equal(plan.metadata.parseStatus, "imported");
    assert.equal(plan.metadata.transactionCount, 1);
    assert.equal("body" in plan.metadata, false);
    assert.equal(plan.commit.insertedRows, 1);
    assert.equal(plan.commit.fifo?.transactionsReplayed, 1);
    assert.equal(plan.commit.insertedTransactions[0]?.sourceHash, plan.metadata.contentHash);
    assert.deepEqual(plan.links, [{
      connectionId: "connection-1",
      messageId: "message-1",
      accountId: "account-1",
      transactionHash: plan.commit.insertedTransactions[0]?.transactionHash,
      ordinal: 1,
    }]);

    const repeated = buildGmailMessageImportPlan(input({
      contentHash: plan.metadata.contentHash,
      accountId: "account-1",
      parseStatus: "imported",
    }));
    assert.deepEqual(repeated, {
      kind: "already_imported",
      connectionId: "connection-1",
      messageId: "message-1",
      contentHash: plan.metadata.contentHash,
    });
  });

  it("同一 message ID 的内容或账户归属变化时明确拒绝覆盖", () => {
    assert.throws(
      () => buildGmailMessageImportPlan(input({
        contentHash: "0".repeat(64),
        accountId: "account-2",
        parseStatus: "imported",
      })),
      (error: unknown) => {
        assert.ok(error instanceof ApplicationError);
        assert.equal(error.code, "GMAIL_MESSAGE_CONFLICT");
        assert.deepEqual(error.details?.changed_fields, ["content_hash", "account_id"]);
        return true;
      },
    );
  });
});
