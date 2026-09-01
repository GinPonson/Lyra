import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { startAgentCliProcess } from "../src/infrastructure/agents/index.js";
import type { AgentCliLaunchPlan } from "../src/infrastructure/agents/index.js";

/** 使用当前 Node 可执行文件构造不依赖外部 CLI 的协议测试计划。 */
function nodePlan(source: string): AgentCliLaunchPlan {
  return {
    provider: "claude",
    protocol: "claude_stream_json",
    executablePath: process.execPath,
    arguments: ["-e", source],
    workDirectory: process.cwd(),
    stdio: "json_lines",
    environmentPolicy: "explicit_credentials_only",
  };
}

describe("Agent CLI process runtime", () => {
  it("通过 stdin/stdout 传递 JSON 帧且不继承宿主环境", async () => {
    const frames: unknown[] = [];
    const handle = startAgentCliProcess({
      plan: nodePlan(`
        let input = "";
        process.stdin.setEncoding("utf8");
        process.stdin.on("data", chunk => input += chunk);
        process.stdin.on("end", () => {
          const frame = JSON.parse(input.trim());
          process.stdout.write(JSON.stringify({
            type: "echo",
            content: frame.content,
            inheritedHome: process.env.HOME !== undefined
          }) + "\\n");
        });
      `),
      environment: { ANTHROPIC_API_KEY: "test-key" },
      timeoutMilliseconds: 2_000,
      onFrame: (frame) => frames.push(frame),
    });

    await handle.sendFrame({ type: "user", content: "分析持仓" });
    handle.finishInput();
    const result = await handle.completion;

    assert.deepEqual(result, {
      status: "completed",
      framesReceived: 1,
      stderrBytes: 0,
    });
    assert.deepEqual(frames, [{
      type: "echo",
      content: "分析持仓",
      inheritedHome: false,
    }]);
    assert.equal(nodePlan("process.exit(0)").arguments.includes("分析持仓"), false);
  });

  it("把非法输出和主动取消转换为不含原始 stderr 的稳定终态", async () => {
    const invalid = startAgentCliProcess({
      plan: nodePlan("process.stdout.write('not-json\\n')"),
      timeoutMilliseconds: 2_000,
      onFrame: () => undefined,
    });
    invalid.finishInput();
    assert.deepEqual(await invalid.completion, {
      status: "failed",
      reason: "INVALID_PROTOCOL_FRAME",
      framesReceived: 0,
      stderrBytes: 0,
    });

    const cancelled = startAgentCliProcess({
      plan: nodePlan("setInterval(() => {}, 1000)"),
      timeoutMilliseconds: 2_000,
      cancelGraceMilliseconds: 20,
      onFrame: () => undefined,
    });
    cancelled.cancel();
    assert.deepEqual(await cancelled.completion, {
      status: "failed",
      reason: "CANCELLED",
      framesReceived: 0,
      stderrBytes: 0,
    });
    assert.throws(
      () => startAgentCliProcess({
        plan: nodePlan("process.exit(0)"),
        environment: { NODE_OPTIONS: "--inspect" },
        timeoutMilliseconds: 2_000,
        onFrame: () => undefined,
      }),
      RangeError,
    );
  });
});
