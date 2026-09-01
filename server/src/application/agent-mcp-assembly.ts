import type {
  AgentSessionSnapshot,
  FinancialToolRegistry,
} from "../domain/agent/index.js";
import { createFinancialToolRegistry } from "./financial-tool-runtime.js";
import type { AgentMcpCredentialRecord } from "../infrastructure/agents/index.js";
import type { AgentMcpCredentialStore } from "../infrastructure/agents/index.js";
import { loadAgentSession } from "../infrastructure/db/agent-session-repository.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";

/** 一次 MCP 请求恢复出的服务端权威上下文。 */
export interface AgentMcpAuthorization {
  session: AgentSessionSnapshot;
  credentialRecord: AgentMcpCredentialRecord;
}

/** MCP 路由需要的生产运行时依赖集合。 */
export interface AgentMcpRuntimeAssembly {
  registry: FinancialToolRegistry;
  loadAuthorization: (tokenHash: string) => Promise<AgentMcpAuthorization | null>;
  persistCredentialRecord: (record: AgentMcpCredentialRecord) => Promise<void>;
}

/**
 * 装配 MCP HTTP 路由的生产依赖：共享金融工具注册表、进程内短期凭据存储和
 * 规范会话恢复。
 *
 * 凭据只在 Agent 回合运行期间由 orchestrator 签发到进程内存储；服务重启即
 * 全部失效，这里不构造任何持久化或长期凭据。会话快照按令牌哈希对应的
 * session ID 从 DuckDB 单次读取，认证校验仍由 MCP 路由执行。
 */
export function assembleAgentMcpRuntime(input: {
  database: LyraDatabase;
  mcpCredentials: AgentMcpCredentialStore;
}): AgentMcpRuntimeAssembly {
  const registry = createFinancialToolRegistry(input.database);
  return {
    registry,
    loadAuthorization: async (tokenHash) => {
      const record = input.mcpCredentials.findByTokenHash(tokenHash);
      if (record === null) {
        return null;
      }
      const session = await loadAgentSession(input.database, record.sessionId);
      if (session === null) {
        return null;
      }
      return { session, credentialRecord: record };
    },
    persistCredentialRecord: async (record) => {
      input.mcpCredentials.update(record);
    },
  };
}
