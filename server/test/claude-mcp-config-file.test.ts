import assert from "node:assert/strict";
import { chmod, mkdtemp, readFile, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { stageClaudeMcpConfig } from "../src/infrastructure/agents/index.js";

const TOKEN = "b".repeat(43);

describe("staged Claude MCP config", () => {
  it("以 0600 私有文件暂存短期 header，并在进程终态幂等清理", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-claude-session-"));
    await chmod(directory, 0o700);
    try {
      const staged = await stageClaudeMcpConfig({
        workDirectory: directory,
        url: "http://127.0.0.1:4310/mcp",
        token: TOKEN,
      });
      const serialized = await readFile(staged.filePath, "utf8");

      assert.equal(staged.filePath.startsWith(`${directory}/.lyra-claude-mcp-`), true);
      assert.equal((await stat(staged.filePath)).mode & 0o777, 0o600);
      assert.deepEqual(JSON.parse(serialized), {
        mcpServers: {
          lyra: {
            type: "http",
            url: "http://127.0.0.1:4310/mcp",
            headers: { Authorization: `Bearer ${TOKEN}` },
          },
        },
      });

      await staged.dispose();
      await staged.dispose();
      await assert.rejects(stat(staged.filePath), { code: "ENOENT" });
    } finally {
      await rm(directory, { recursive: true });
    }
  });

  it("拒绝在 group 或 other 可访问的目录中写入会话凭据", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-claude-public-"));
    try {
      await chmod(directory, 0o755);
      await assert.rejects(
        stageClaudeMcpConfig({
          workDirectory: directory,
          url: "https://lyra.example.test/mcp",
          token: TOKEN,
        }),
        RangeError,
      );
    } finally {
      await rm(directory, { recursive: true });
    }
  });
});
