import { randomUUID } from "node:crypto";
import { rm, stat, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";

import { buildClaudeHttpMcpConfig } from "./mcp-http-config.js";

/** Claude 子进程存活期间使用的私有 MCP 配置文件句柄。 */
export interface StagedClaudeMcpConfig {
  filePath: string;
  /** 子进程退出后幂等删除包含短期 Bearer 凭据的配置文件。 */
  dispose(): Promise<void>;
}

/**
 * 在既有 Agent 私有会话目录中暂存 Claude `--mcp-config` 文件。
 *
 * 目录必须已经存在、属于调用方管理且没有 group/other 权限；文件使用随机名、
 * 排他创建和 0600 权限。调用方必须把返回路径交给启动计划，并在进程终态调用
 * dispose；配置正文不得进入日志、异常详情或数据库。
 */
export async function stageClaudeMcpConfig(input: {
  workDirectory: string;
  url: string;
  token: string;
}): Promise<StagedClaudeMcpConfig> {
  const workDirectory = resolve(input.workDirectory);
  const directoryStat = await stat(workDirectory);
  if (!directoryStat.isDirectory()) {
    throw new RangeError("Claude Agent 工作路径必须是目录");
  }
  if ((directoryStat.mode & 0o077) !== 0) {
    throw new RangeError("Claude Agent 工作目录必须禁止 group/other 访问");
  }

  const filePath = join(workDirectory, `.lyra-claude-mcp-${randomUUID()}.json`);
  const config = buildClaudeHttpMcpConfig({ url: input.url, token: input.token });
  await writeFile(filePath, JSON.stringify(config), {
    encoding: "utf8",
    flag: "wx",
    mode: 0o600,
  });

  let disposed = false;
  return {
    filePath,
    async dispose(): Promise<void> {
      if (disposed) {
        return;
      }
      await rm(filePath, { force: true });
      disposed = true;
    },
  };
}
