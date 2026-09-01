import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { buildAgentCliLaunchPlan } from "../src/infrastructure/agents/index.js";

describe("Agent CLI launch plan", () => {
  it("为 Claude 和 Pi 生成只加载 Lyra 工具的机器协议 argv", () => {
    const claude = buildAgentCliLaunchPlan({
      provider: "claude",
      executablePath: "/opt/lyra/bin/claude",
      workDirectory: "/var/lib/lyra/agents/session-1",
      sessionId: "018f14a0-7b32-7c9d-9000-000000000001",
      model: "sonnet",
      integrationPath: "/etc/lyra/claude-mcp.json",
    });
    assert.equal(claude.protocol, "claude_stream_json");
    assert.equal(claude.arguments.includes("--bare"), true);
    assert.equal(claude.arguments.includes("--strict-mcp-config"), true);
    assert.equal(claude.arguments.includes("--tools"), true);
    assert.deepEqual(
      claude.arguments.slice(
        claude.arguments.indexOf("--allowedTools"),
        claude.arguments.indexOf("--allowedTools") + 2,
      ),
      ["--allowedTools", "mcp__lyra__*"],
    );
    assert.equal(claude.arguments.includes("--model"), true);
    assert.equal(claude.environmentPolicy, "explicit_credentials_only");

    const pi = buildAgentCliLaunchPlan({
      provider: "pi",
      executablePath: "/opt/lyra/bin/pi",
      workDirectory: "/var/lib/lyra/agents/session-1",
      sessionId: "session-1",
      integrationPath: "/opt/lyra/agent/pi-extension.js",
      sessionDirectory: "/var/lib/lyra/agents/pi-sessions",
    });
    assert.equal(pi.protocol, "pi_rpc");
    assert.equal(pi.arguments.includes("--no-builtin-tools"), true);
    assert.equal(pi.arguments.includes("--no-extensions"), true);
    assert.equal(pi.arguments.includes("--extension"), true);
    assert.equal(pi.arguments.includes("--no-context-files"), true);
  });

  it("OpenCode 只使用 pure ACP 隔离目录，并拒绝相对路径和无关集成参数", () => {
    const opencode = buildAgentCliLaunchPlan({
      provider: "opencode",
      executablePath: "/opt/lyra/bin/opencode",
      workDirectory: "/var/lib/lyra/agents/session-2",
      sessionId: "session-2",
    });
    assert.deepEqual(opencode.arguments, [
      "acp", "--pure", "--cwd", "/var/lib/lyra/agents/session-2", "--log-level", "ERROR",
    ]);
    assert.equal(opencode.arguments.some((argument) => argument.includes("prompt")), false);

    assert.throws(
      () => buildAgentCliLaunchPlan({
        provider: "claude",
        executablePath: "claude",
        workDirectory: "/tmp/session",
        sessionId: "session",
        integrationPath: "/tmp/mcp.json",
      }),
      RangeError,
    );
    assert.throws(
      () => buildAgentCliLaunchPlan({
        provider: "opencode",
        executablePath: "/opt/lyra/bin/opencode",
        workDirectory: "/tmp/session",
        sessionId: "session",
        integrationPath: "/tmp/unused.json",
      }),
      RangeError,
    );
  });
});
