/** 服务端传输层公共导出。 */
export { serializeApiError } from "./api-error.js";
export { registerAgentMcpHttpRoutes } from "./agent-mcp-http.js";
export { buildHttpServer } from "./http-server.js";
export {
  LYRA_PROTOCOL_VERSION,
  MAX_SUPPORTED_PROTOCOL_VERSION,
  MIN_SUPPORTED_PROTOCOL_VERSION,
  assertClientProtocolVersion,
  createServerProtocolMetadata,
} from "./protocol-version.js";
export type { ApiErrorBody, SerializedApiError } from "./api-error.js";
export type {
  AgentMcpAuthorizationSnapshot,
  AgentMcpHttpOptions,
} from "./agent-mcp-http.js";
export type { HttpServerOptions } from "./http-server.js";
export type { ServerProtocolMetadata } from "./protocol-version.js";
