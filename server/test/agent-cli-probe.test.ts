import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { probeAgentCli } from "../src/infrastructure/agents/index.js";

describe("Agent CLI availability probe", () => {
  it("在隔离目录中识别可执行文件版本", async () => {
    const nodeVersion = process.version.replace(/^v/, "");
    const result = await probeAgentCli({
      provider: "claude",
      executablePath: process.execPath,
      versionArguments: ["--version"],
      allowedVersions: [nodeVersion],
      timeoutMilliseconds: 2_000,
    });

    assert.equal(result.status, "available");
    if (result.status === "available") {
      assert.match(result.version, /^\d+\.\d+\.\d+/);
    }
  });

  it("把不存在和超时进程转换为稳定失败原因", async () => {
    const missing = await probeAgentCli({
      provider: "pi",
      executablePath: "/definitely-missing/lyra-pi",
      versionArguments: ["--version"],
      allowedVersions: ["0.84.2"],
      timeoutMilliseconds: 1_000,
    });
    const timeout = await probeAgentCli({
      provider: "opencode",
      executablePath: process.execPath,
      versionArguments: ["-e", "setTimeout(() => {}, 10_000)"],
      allowedVersions: ["1.18.21"],
      timeoutMilliseconds: 20,
    });

    assert.deepEqual(missing, {
      provider: "pi",
      status: "unavailable",
      reason: "EXECUTABLE_NOT_FOUND",
    });
    assert.deepEqual(timeout, {
      provider: "opencode",
      status: "unavailable",
      reason: "PROCESS_TIMEOUT",
    });
  });

  it("把未通过适配器契约验证的版本明确标记为不可用", async () => {
    const result = await probeAgentCli({
      provider: "claude",
      executablePath: process.execPath,
      versionArguments: ["--version"],
      allowedVersions: ["2.1.233"],
      timeoutMilliseconds: 2_000,
    });

    assert.deepEqual(result, {
      provider: "claude",
      status: "unavailable",
      reason: "VERSION_NOT_ALLOWED",
    });
  });
});
