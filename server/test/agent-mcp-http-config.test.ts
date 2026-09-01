import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  buildClaudeHttpMcpConfig,
  buildOpenCodeHttpMcpServer,
} from "../src/infrastructure/agents/index.js";

const TOKEN = "a".repeat(43);

describe("Agent MCP HTTP provider config", () => {
  it("只通过 Authorization header 向 Claude 和 OpenCode 传递短期凭据", () => {
    const claude = buildClaudeHttpMcpConfig({
      url: "http://127.0.0.1:4310/mcp",
      token: TOKEN,
    });
    const openCode = buildOpenCodeHttpMcpServer({
      url: "https://lyra.example.test/mcp",
      token: TOKEN,
    });

    assert.deepEqual(claude, {
      mcpServers: {
        lyra: {
          type: "http",
          url: "http://127.0.0.1:4310/mcp",
          headers: { Authorization: `Bearer ${TOKEN}` },
        },
      },
    });
    assert.deepEqual(openCode, {
      type: "http",
      name: "lyra",
      url: "https://lyra.example.test/mcp",
      headers: [{ name: "Authorization", value: `Bearer ${TOKEN}` }],
    });
    assert.equal(claude.mcpServers.lyra.url.includes(TOKEN), false);
    assert.equal(openCode.url.includes(TOKEN), false);
  });

  it("拒绝非回环明文地址、URL 内嵌凭据和非法会话令牌", () => {
    assert.throws(
      () => buildClaudeHttpMcpConfig({
        url: "http://192.0.2.10/mcp",
        token: TOKEN,
      }),
      RangeError,
    );
    assert.throws(
      () => buildOpenCodeHttpMcpServer({
        url: "https://user:secret@lyra.example.test/mcp#token",
        token: TOKEN,
      }),
      RangeError,
    );
    assert.throws(
      () => buildOpenCodeHttpMcpServer({
        url: "http://localhost:4310/mcp",
        token: "short\r\nAuthorization: injected",
      }),
      RangeError,
    );
  });
});
