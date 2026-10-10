import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { ApplicationError } from "../src/application/index.js";
import {
  AgentSessionStateError,
  AgentToolPolicyError,
} from "../src/domain/agent/index.js";
import { FifoError } from "../src/domain/fifo/index.js";
import { IngestError } from "../src/domain/ledger/index.js";
import { DeviceAuthError } from "../src/infrastructure/security/index.js";
import { serializeApiError } from "../src/transport/index.js";

describe("API error protocol", () => {
  it("保留显式应用错误的稳定字段", () => {
    const result = serializeApiError(new ApplicationError({
      code: "IMPORT_PREVIEW_STALE",
      message: "导入预览已失效，请重新预览",
      statusCode: 409,
      retryable: false,
      details: { expected_ledger_version: 12, actual_ledger_version: 13 },
    }), "request-1");

    assert.deepEqual(result, {
      statusCode: 409,
      body: {
        request_id: "request-1",
        code: "IMPORT_PREVIEW_STALE",
        message: "导入预览已失效，请重新预览",
        details: { expected_ledger_version: 12, actual_ledger_version: 13 },
        retryable: false,
      },
    });
  });

  it("将已知领域错误映射为客户端可处理的错误码", () => {
    const cases: ReadonlyArray<readonly [unknown, number, string]> = [
      [new IngestError("第 4 行金额无法勾稽"), 422, "IMPORT_VALIDATION_FAILED"],
      [new FifoError("账户 AAPL 库存不足"), 409, "LEDGER_REBUILD_FAILED"],
      [new DeviceAuthError("PAIRING_CODE_EXPIRED", "配对码已经过期"), 410, "PAIRING_CODE_EXPIRED"],
      [new AgentToolPolicyError("模型请求了 shell"), 403, "AGENT_TOOL_FORBIDDEN"],
      [new AgentSessionStateError("running", "closed"), 409, "AGENT_SESSION_STATE_CONFLICT"],
      [new RangeError("limit 必须为正整数"), 400, "INVALID_ARGUMENT"],
    ];

    for (const [error, expectedStatus, expectedCode] of cases) {
      const result = serializeApiError(error, "request-known");
      assert.equal(result.statusCode, expectedStatus);
      assert.equal(result.body.code, expectedCode);
      assert.equal(result.body.request_id, "request-known");
    }
  });

  it("未知异常返回通用错误且不泄露底层敏感信息", () => {
    const sensitive = "token=secret-value path=/private/ledger.duckdb";
    const result = serializeApiError(new Error(sensitive), "request-unknown");
    const serialized = JSON.stringify(result);

    assert.equal(result.statusCode, 500);
    assert.equal(result.body.code, "INTERNAL_ERROR");
    assert.equal(result.body.retryable, false);
    assert.equal(serialized.includes("secret-value"), false);
    assert.equal(serialized.includes("ledger.duckdb"), false);

    const protocol = serializeApiError(
      new ApplicationError({ code: "AGENT_TURN_FAILED", message: "AI 请求失败", statusCode: 502, retryable: true, cause: new Error("token=provider-secret path=/private/runtime") }),
      "request-protocol",
    );
    const serializedProtocol = JSON.stringify(protocol);
    assert.equal(protocol.statusCode, 502);
    assert.equal(protocol.body.code, "AGENT_TURN_FAILED");
    assert.equal(protocol.body.retryable, true);
    assert.equal(serializedProtocol.includes("provider-secret"), false);
    assert.equal(serializedProtocol.includes("runtime"), false);
  });
});
