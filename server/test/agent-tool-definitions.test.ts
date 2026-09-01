import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  FINANCIAL_TOOL_DEFINITIONS,
  FINANCIAL_TOOL_NAMES,
  FinancialToolRegistry,
} from "../src/domain/agent/index.js";
import { CASH_ACTIONS, POSITION_ACTIONS } from "../src/domain/ledger/index.js";

describe("financial tool definitions", () => {
  it("以单一有序清单公开 17 个非破坏性闭域工具", () => {
    assert.equal(FINANCIAL_TOOL_DEFINITIONS.length, 17);
    assert.deepEqual(
      FINANCIAL_TOOL_DEFINITIONS.map((definition) => definition.name),
      FINANCIAL_TOOL_NAMES,
    );
    assert.equal(new Set(FINANCIAL_TOOL_NAMES).size, FINANCIAL_TOOL_NAMES.length);

    for (const definition of FINANCIAL_TOOL_DEFINITIONS) {
      assert.equal(definition.description.length > 0, true);
      assert.equal(definition.inputSchema.type, "object");
      assert.equal(definition.inputSchema.additionalProperties, false);
      assert.deepEqual(definition.annotations, {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      });
      assert.equal("accountScope" in definition.inputSchema.properties, false);
      assert.equal("ledgerVersion" in definition.inputSchema.properties, false);
      assert.equal("asOf" in definition.inputSchema.properties, false);
    }
    for (const toolName of ["trades", "cashflow"] as const) {
      const definition = FINANCIAL_TOOL_DEFINITIONS.find((item) => item.name === toolName);
      const properties = definition?.inputSchema.properties as Readonly<
        Record<string, Readonly<Record<string, unknown>>>
      >;
      assert.deepEqual(properties["sourceType"]?.["enum"], [
        "csv", "excel", "gmail",
      ]);
    }
    for (const toolName of ["trades", "realized", "cashflow"]) {
      const definition = FINANCIAL_TOOL_DEFINITIONS.find((item) => item.name === toolName);
      const properties = definition?.inputSchema.properties as Readonly<
        Record<string, Readonly<Record<string, unknown>>>
      >;
      assert.equal(properties["limit"]?.["maximum"], 200);
      assert.equal(properties["cursor"]?.["maxLength"], 1024);
    }
    const trades = FINANCIAL_TOOL_DEFINITIONS.find((item) => item.name === "trades");
    assert.deepEqual(trades?.inputSchema.properties["action"]?.["enum"], [
      ...POSITION_ACTIONS,
      ...CASH_ACTIONS,
    ]);
  });

  it("仅为实际配置 handler 的工具生成 MCP 公开定义", () => {
    const registry = new FinancialToolRegistry({
      summary: async () => ({}),
      risk: async () => ({}),
    });

    assert.deepEqual(registry.availableTools(), ["summary", "risk"]);
    assert.deepEqual(
      registry.availableToolDefinitions().map((definition) => definition.name),
      ["summary", "risk"],
    );
    assert.equal(
      registry.availableToolDefinitions()[0]?.inputSchema.additionalProperties,
      false,
    );
  });
});
