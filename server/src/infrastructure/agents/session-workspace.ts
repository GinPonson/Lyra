import { chmod, mkdtemp, rm, stat } from "node:fs/promises";
import { isAbsolute, join } from "node:path";

/** 单个规范 Agent 会话独占的空工作目录。 */
export interface AgentSessionWorkspace {
  directory: string;
  /** 会话关闭后幂等删除目录及其中的短期集成配置。 */
  dispose(): Promise<void>;
}

/**
 * 在配置的 Agent 根目录下创建随机、0700 的会话工作目录。
 *
 * 不使用逻辑会话 ID、用户名或 prompt 拼接路径。该目录是三个供应商启动计划
 * 唯一允许使用的 cwd，也可承载 Claude 临时 MCP 配置和 Pi 原生会话文件。
 */
export async function createAgentSessionWorkspace(
  workspaceRoot: string,
): Promise<AgentSessionWorkspace> {
  if (!isAbsolute(workspaceRoot)) {
    throw new RangeError("Agent 工作根目录必须是绝对路径");
  }
  const rootStat = await stat(workspaceRoot);
  if (!rootStat.isDirectory()) {
    throw new RangeError("Agent 工作根路径必须是目录");
  }

  const directory = await mkdtemp(join(workspaceRoot, ".lyra-agent-session-"));
  try {
    await chmod(directory, 0o700);
  } catch (error: unknown) {
    try {
      await rm(directory, { recursive: true });
    } catch (cleanupError: unknown) {
      throw new AggregateError(
        [error, cleanupError],
        "Agent 会话目录权限设置失败且清理失败",
      );
    }
    throw error;
  }

  let disposed = false;
  return {
    directory,
    async dispose(): Promise<void> {
      if (disposed) {
        return;
      }
      await rm(directory, { recursive: true });
      disposed = true;
    },
  };
}
