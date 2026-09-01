import type { OpenCodeHttpMcpServer } from "./opencode-acp-adapter.js";

/** Claude Code `--mcp-config` 接受的唯一 Lyra HTTP MCP 配置。 */
export interface ClaudeHttpMcpConfig {
  mcpServers: {
    lyra: {
      type: "http";
      url: string;
      headers: { Authorization: string };
    };
  };
}

/**
 * 构造 Claude Code 2.1.x 可读取的内存 MCP 配置对象。
 *
 * 调用方如需写入 `--mcp-config` 文件，必须使用会话私有目录和 0600 权限，并在
 * 子进程退出时删除；本函数不会把凭据放入 URL、argv、日志或持久化记录。
 */
export function buildClaudeHttpMcpConfig(input: {
  url: string;
  token: string;
}): ClaudeHttpMcpConfig {
  const url = normalizeAgentMcpHttpUrl(input.url);
  const authorization = buildAuthorizationHeader(input.token);
  return {
    mcpServers: {
      lyra: {
        type: "http",
        url,
        headers: { Authorization: authorization },
      },
    },
  };
}

/** 构造 OpenCode ACP `session/new` 使用的唯一 Lyra HTTP MCP 描述。 */
export function buildOpenCodeHttpMcpServer(input: {
  url: string;
  token: string;
}): OpenCodeHttpMcpServer {
  return {
    type: "http",
    name: "lyra",
    url: normalizeAgentMcpHttpUrl(input.url),
    headers: [{ name: "Authorization", value: buildAuthorizationHeader(input.token) }],
  };
}

/**
 * 规范 Agent 子进程可访问的 MCP URL。
 *
 * 远端连接必须使用 HTTPS；单机部署可使用回环 HTTP。URL 禁止携带用户信息和
 * fragment，避免短期凭据被复制进日志或代理访问记录。
 */
export function normalizeAgentMcpHttpUrl(value: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch (error) {
    throw new RangeError("Lyra MCP URL 无效", { cause: error });
  }
  const isLoopbackHttp = url.protocol === "http:"
    && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
  if (url.protocol !== "https:" && !isLoopbackHttp) {
    throw new RangeError("Lyra MCP 必须使用 HTTPS 或回环 HTTP");
  }
  if (url.username.length > 0 || url.password.length > 0 || url.hash.length > 0) {
    throw new RangeError("Lyra MCP URL 禁止内嵌凭据或片段");
  }
  return url.toString();
}

/** 只接受签发器生成的 Base64URL 不透明令牌，并构造 Bearer header。 */
function buildAuthorizationHeader(token: string): string {
  if (!/^[A-Za-z0-9_-]{40,}$/.test(token)) {
    throw new RangeError("Lyra MCP 会话凭据格式无效");
  }
  return `Bearer ${token}`;
}
