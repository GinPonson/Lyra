import type {
  AgentProvider,
  AgentSessionSnapshot,
  AgentSessionStatus,
  NormalizedAgentMessage,
} from "../domain/agent/index.js";
import type { AccountScope } from "../domain/analytics/index.js";

/** App 可展示的规范 Agent 消息，不包含隐藏推理或供应商协议状态。 */
export interface PublicAgentMessage {
  id: string;
  role: NormalizedAgentMessage["role"];
  content: string;
  createdAt: string;
  status?: NormalizedAgentMessage["status"];
  errorCode?: string | null;
}

/** App 重连和会话页面共用的权威会话快照。 */
export interface PublicAgentSession {
  id: string;
  provider: AgentProvider;
  status: AgentSessionStatus;
  accountScope: AccountScope;
  ledgerVersion: number;
  asOf: string;
  revision: number;
  messages: readonly PublicAgentMessage[];
}

/**
 * 将持久化 Agent 会话投影为 App 可公开的快照。
 *
 * 逐字段重建会话、账户范围与消息，保证内部运行状态以及未来新增的
 * 供应商私有字段不会因对象透传进入客户端响应。
 *
 * @param session - 已从仓储恢复的规范 Agent 会话快照。
 * @returns 可安全序列化给 App 的独立快照。
 */
export function createPublicAgentSession(
  session: AgentSessionSnapshot,
): PublicAgentSession {
  return {
    id: session.id,
    provider: session.provider,
    status: session.status,
    accountScope: session.accountScope.kind === "all"
      ? { kind: "all" }
      : { kind: "account", account: session.accountScope.account },
    ledgerVersion: session.ledgerVersion,
    asOf: session.asOf,
    revision: session.revision,
    messages: session.messages.map(createPublicAgentMessage),
  };
}

/** 逐字段复制单条公开消息，并保留可见终态字段。 */
function createPublicAgentMessage(
  message: NormalizedAgentMessage,
): PublicAgentMessage {
  const result: PublicAgentMessage = {
    id: message.id,
    role: message.role,
    content: message.content,
    createdAt: message.createdAt,
  };
  if (message.status !== undefined) {
    result.status = message.status;
  }
  if (message.errorCode !== undefined) {
    result.errorCode = message.errorCode;
  }
  return result;
}
