import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { Decimal } from "decimal.js";

import {
  IngestError,
  parseAccount,
  parseDateField,
  parseMoney,
  parseOptionSymbol,
} from "../src/domain/ledger/index.js";

describe("parseMoney", () => {
  it("解析 Schwab 美元、千位分隔和空字段", () => {
    assert.equal(parseMoney("$1,234.56")?.equals(new Decimal("1234.56")), true);
    assert.equal(parseMoney("-$1865.66")?.equals(new Decimal("-1865.66")), true);
    assert.equal(parseMoney("$0.66")?.equals(new Decimal("0.66")), true);
    assert.equal(parseMoney(""), null);
  });

  it("拒绝非法十进制文本", () => {
    assert.throws(() => parseMoney("abc"), IngestError);
  });
});

describe("parseDateField", () => {
  it("解析普通日期", () => {
    assert.deepEqual(parseDateField("07/28/2026"), {
      recordDate: "2026-07-28",
      transactionDate: "2026-07-28",
    });
  });

  it("保留 as-of 前的记录日期并采用后一个生效日期", () => {
    assert.deepEqual(parseDateField("05/18/2026 as of 05/15/2026"), {
      recordDate: "2026-05-18",
      transactionDate: "2026-05-15",
    });
  });

  it("拒绝未知格式和不存在的日历日期", () => {
    assert.throws(() => parseDateField("2026-07-28"), IngestError);
    assert.throws(() => parseDateField("02/30/2026"), IngestError);
  });
});

describe("parseOptionSymbol", () => {
  it("解析看涨和看跌期权", () => {
    const call = parseOptionSymbol("GOOG 03/19/2027 385.00 C");
    assert.equal(call?.underlying, "GOOG");
    assert.equal(call?.expiry, "2027-03-19");
    assert.equal(call?.strike.equals(new Decimal("385.00")), true);
    assert.equal(call?.optionType, "C");

    const put = parseOptionSymbol("INTC 05/15/2026 55.00 P");
    assert.equal(put?.optionType, "P");
  });

  it("把普通股票代码留给交易动作分类", () => {
    assert.equal(parseOptionSymbol("INTC"), null);
  });
});

describe("parseAccount", () => {
  it("从 Schwab 官方文件名提取账户标识", () => {
    assert.equal(
      parseAccount("Individual_XXX276_Transactions_20260730-084033.csv"),
      "XXX276",
    );
  });

  it("拒绝来源不明的文件名", () => {
    assert.throws(() => parseAccount("random.csv"), IngestError);
  });
});
