/** 服务端 Agent CLI 基础设施公共导出。 */
export { probeAgentCli } from "./cli-probe.js";
export {
  CLAUDE_LYRA_MCP_TOOL_PATTERN,
  ClaudeStreamProtocolError,
  buildClaudeInterruptRequest,
  buildClaudeToolPermissionResponse,
  buildClaudeUserMessage,
  parseClaudeStreamFrame,
} from "./claude-stream-adapter.js";
export { buildAgentCliLaunchPlan } from "./launch-plan.js";
export { stageClaudeMcpConfig } from "./claude-mcp-config-file.js";
export { AgentMcpCredentialStore } from "./mcp-credential-store.js";
export {
  authenticateAgentMcpCredential,
  issueAgentMcpCredential,
  revokeAgentMcpCredential,
} from "./mcp-session-credential.js";
export {
  buildClaudeHttpMcpConfig,
  buildOpenCodeHttpMcpServer,
  normalizeAgentMcpHttpUrl,
} from "./mcp-http-config.js";
export { startAgentCliProcess } from "./process-runtime.js";
export { createAgentSessionWorkspace } from "./session-workspace.js";
export {
  OPENCODE_ACP_PROTOCOL_VERSION,
  OpenCodeAcpProtocolError,
  OpenCodeAcpTurnAdapter,
  buildOpenCodeCancelNotification,
  buildOpenCodeInitializeRequest,
  buildOpenCodeNewSessionRequest,
  buildOpenCodePromptRequest,
  parseOpenCodeInitializeResponse,
  parseOpenCodeNewSessionResponse,
} from "./opencode-acp-adapter.js";
export {
  PiRpcProtocolError,
  buildPiAbortCommand,
  buildPiPromptCommand,
  parsePiRpcFrame,
} from "./pi-rpc-adapter.js";
export type {
  ClaudeInterruptRequest,
  ClaudeStreamAdapterSignal,
  ClaudeToolPermissionResponse,
  ClaudeUserMessage,
} from "./claude-stream-adapter.js";
export type {
  AgentCliProbeFailure,
  AgentCliProbeInput,
  AgentCliProbeResult,
} from "./cli-probe.js";
export type {
  AgentMcpCredentialRecord,
  AuthenticatedAgentMcpCredential,
  IssuedAgentMcpCredential,
} from "./mcp-session-credential.js";
export type { ClaudeHttpMcpConfig } from "./mcp-http-config.js";
export type { StagedClaudeMcpConfig } from "./claude-mcp-config-file.js";
export type {
  AgentCliLaunchPlan,
  AgentCliProtocol,
} from "./launch-plan.js";
export type {
  OpenCodeAcpAdapterSignal,
  OpenCodeAcpRequestId,
  OpenCodeCancelNotification,
  OpenCodeHttpMcpServer,
  OpenCodeInitializeRequest,
  OpenCodeInitializeResult,
  OpenCodeNewSessionRequest,
  OpenCodeNewSessionResult,
  OpenCodePromptRequest,
} from "./opencode-acp-adapter.js";
export type {
  AgentCliProcessHandle,
  AgentCliRuntimeFailure,
  AgentCliRuntimeResult,
} from "./process-runtime.js";
export type { AgentSessionWorkspace } from "./session-workspace.js";
export type {
  PiAbortCommand,
  PiPromptCommand,
  PiRpcAdapterSignal,
} from "./pi-rpc-adapter.js";
