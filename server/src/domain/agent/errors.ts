import type { AgentProvider } from "./switching.js";

/** Agent MCP 凭据失败的内部分类；客户端只接收统一未授权错误。 */
export type AgentMcpCredentialFailure =
  | "invalid_token"
  | "expired"
  | "revoked"
  | "session_not_running"
  | "session_mismatch"
  | "scope_mismatch";

/** Agent 子进程访问内部 MCP 时没有满足当前会话授权边界。 */
export class AgentMcpCredentialError extends Error {
  readonly code = "AGENT_MCP_UNAUTHORIZED" as const;

  /**
   * @param internalReason - 仅供服务端审计定位，API 不区分令牌或范围失败。
   */
  constructor(readonly internalReason: AgentMcpCredentialFailure) {
    super("Agent MCP 凭据无效");
    this.name = "AgentMcpCredentialError";
  }
}

/** 供应商协议帧、版本或交互能力不符合 Lyra 约束。 */
export class AgentProtocolError extends Error {
  readonly code = "AGENT_PROTOCOL_ERROR" as const;

  /**
   * @param provider - 发生协议异常的供应商。
   * @param internalReason - 仅供服务端日志定位，不得进入 API 或 WebSocket 错误正文。
   */
  constructor(
    readonly provider: AgentProvider,
    readonly internalReason: string,
  ) {
    super(`${provider} Agent 协议响应无效`);
    this.name = "AgentProtocolError";
  }
}
