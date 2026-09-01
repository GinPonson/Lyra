import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { ApplicationError } from "../src/application/index.js";
import {
  assertClientProtocolVersion,
  createServerProtocolMetadata,
} from "../src/transport/index.js";

describe("client/server protocol version", () => {
  it("接受当前协议并生成公开握手元数据", () => {
    assert.equal(assertClientProtocolVersion("1"), 1);
    assert.deepEqual(createServerProtocolMetadata("0.1.0", 5), {
      server_version: "0.1.0",
      protocol_version: 1,
      min_supported_protocol_version: 1,
      max_supported_protocol_version: 1,
      schema_version: 5,
    });
  });

  it("用稳定 426 错误拒绝缺失、非法及超出范围的协议版本", () => {
    for (const rawVersion of [undefined, "abc", "0", "2"]) {
      assert.throws(
        () => assertClientProtocolVersion(rawVersion),
        (error: unknown) => (
          error instanceof ApplicationError
          && error.code === "CLIENT_VERSION_INCOMPATIBLE"
          && error.statusCode === 426
        ),
      );
    }
  });
});
