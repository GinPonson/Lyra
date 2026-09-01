import assert from "node:assert/strict";
import { mkdtemp, readdir, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { createAgentSessionWorkspace } from "../src/infrastructure/agents/index.js";

describe("Agent session workspace", () => {
  it("创建随机 0700 空目录，并在会话关闭时幂等删除全部临时内容", async () => {
    const root = await mkdtemp(join(tmpdir(), "lyra-agent-root-"));
    try {
      const workspace = await createAgentSessionWorkspace(root);

      assert.equal(workspace.directory.startsWith(`${root}/.lyra-agent-session-`), true);
      assert.equal((await stat(workspace.directory)).mode & 0o777, 0o700);
      assert.deepEqual(await readdir(workspace.directory), []);

      await writeFile(join(workspace.directory, "temporary.json"), "{}", { mode: 0o600 });
      await workspace.dispose();
      await workspace.dispose();
      await assert.rejects(stat(workspace.directory), { code: "ENOENT" });
    } finally {
      await rm(root, { recursive: true });
    }
  });

  it("拒绝相对路径和非目录根路径", async () => {
    await assert.rejects(createAgentSessionWorkspace("relative/agents"), RangeError);

    const root = await mkdtemp(join(tmpdir(), "lyra-agent-root-file-"));
    const filePath = join(root, "not-a-directory");
    try {
      await writeFile(filePath, "not a directory");
      await assert.rejects(createAgentSessionWorkspace(filePath), RangeError);
    } finally {
      await rm(root, { recursive: true });
    }
  });
});
