import { randomUUID } from "node:crypto";

import {
  AgentMcpCredentialError,
} from "../../domain/agent/index.js";
import type {
  AgentProvider,
  AgentSessionSnapshot,
  AgentToolContext,
} from "../../domain/agent/index.js";
import type { AccountScope } from "../../domain/analytics/index.js";
import {
  createOpaqueCredential,
  verifyOpaqueCredential,
} from "../security/index.js";

/** 单个 Agent 原生进程访问 Lyra MCP 时保存的哈希凭据记录。 */
export interface AgentMcpCredentialRecord {
  id: string;
  sessionId: string;
  sessionRevision: number;
  provider: AgentProvider;
  accountScope: AccountScope;
  ledgerVersion: number;
  asOf: string;
  tokenHash: string;
  status: "active" | "revoked";
  expiresAt: string;
  issuedAt: string;
  lastUsedAt: string | null;
  revokedAt: string | null;
}

/** 签发时唯一一次返回给受控子进程配置的明文 MCP 凭据。 */
export interface IssuedAgentMcpCredential {
  token: string;
  record: AgentMcpCredentialRecord;
}

/** MCP 请求认证后的不可变工具上下文与更新记录。 */
export interface AuthenticatedAgentMcpCredential {
  context: AgentToolContext;
  record: AgentMcpCredentialRecord;
}

/**
 * 为当前 running 回合签发短期 Agent MCP 凭据。
 *
 * 凭据同时绑定逻辑会话、revision、供应商、账户范围、账本版本和截止时间；
 * 明文仅进入子进程的内存配置，服务端记录只保存 SHA-256 哈希。
 */
export function issueAgentMcpCredential(input: {
  session: AgentSessionSnapshot;
  now: Date;
  ttlMilliseconds: number;
}): IssuedAgentMcpCredential {
  assertPositiveTtl(input.ttlMilliseconds);
  if (input.session.status !== "running") {
    throw new AgentMcpCredentialError("session_not_running");
  }
  const credential = createOpaqueCredential();
  const issuedAt = input.now.toISOString();
  return {
    token: credential.token,
    record: {
      id: randomUUID(),
      sessionId: input.session.id,
      sessionRevision: input.session.revision,
      provider: input.session.provider,
      accountScope: copyAccountScope(input.session.accountScope),
      ledgerVersion: input.session.ledgerVersion,
      asOf: input.session.asOf,
      tokenHash: credential.tokenHash,
      status: "active",
      expiresAt: new Date(input.now.getTime() + input.ttlMilliseconds).toISOString(),
      issuedAt,
      lastUsedAt: null,
      revokedAt: null,
    },
  };
}

/**
 * 校验 MCP Bearer 凭据并恢复服务端权威工具上下文。
 *
 * 任一令牌、时效、会话、供应商或数据口径不匹配都使用统一公开错误；详细
 * 分类只保留在 internalReason，避免给子进程提供凭据探测信息。
 */
export function authenticateAgentMcpCredential(input: {
  session: AgentSessionSnapshot;
  record: AgentMcpCredentialRecord;
  token: string;
  now: Date;
}): AuthenticatedAgentMcpCredential {
  if (!verifyOpaqueCredential(input.token, input.record.tokenHash)) {
    throw new AgentMcpCredentialError("invalid_token");
  }
  if (input.record.status !== "active") {
    throw new AgentMcpCredentialError("revoked");
  }
  if (input.now.getTime() >= Date.parse(input.record.expiresAt)) {
    throw new AgentMcpCredentialError("expired");
  }
  if (input.session.status !== "running") {
    throw new AgentMcpCredentialError("session_not_running");
  }
  if (
    input.record.sessionId !== input.session.id
    || input.record.sessionRevision !== input.session.revision
    || input.record.provider !== input.session.provider
  ) {
    throw new AgentMcpCredentialError("session_mismatch");
  }
  if (
    input.record.ledgerVersion !== input.session.ledgerVersion
    || input.record.asOf !== input.session.asOf
    || !sameAccountScope(input.record.accountScope, input.session.accountScope)
  ) {
    throw new AgentMcpCredentialError("scope_mismatch");
  }
  return {
    context: {
      accountScope: copyAccountScope(input.session.accountScope),
      ledgerVersion: input.session.ledgerVersion,
      asOf: input.session.asOf,
    },
    record: { ...input.record, lastUsedAt: input.now.toISOString() },
  };
}

/** 回合完成、取消或进程释放时幂等撤销当前会话的 MCP 凭据。 */
export function revokeAgentMcpCredential(
  record: AgentMcpCredentialRecord,
  sessionId: string,
  now: Date,
): AgentMcpCredentialRecord {
  if (record.sessionId !== sessionId) {
    throw new AgentMcpCredentialError("session_mismatch");
  }
  if (record.status === "revoked") {
    return record;
  }
  const revokedAt = now.toISOString();
  return { ...record, status: "revoked", revokedAt };
}

/** 比较显式账户范围，不使用字符串序列化或隐式默认范围。 */
function sameAccountScope(left: AccountScope, right: AccountScope): boolean {
  if (left.kind !== right.kind) {
    return false;
  }
  return left.kind === "all"
    || (right.kind === "account" && left.account === right.account);
}

/** 复制账户范围，防止调用方修改会话对象后影响已签发记录。 */
function copyAccountScope(scope: AccountScope): AccountScope {
  return scope.kind === "all" ? { kind: "all" } : { kind: "account", account: scope.account };
}

/** 校验 MCP 凭据有效期。 */
function assertPositiveTtl(ttlMilliseconds: number): void {
  if (!Number.isSafeInteger(ttlMilliseconds) || ttlMilliseconds <= 0) {
    throw new RangeError("Agent MCP 凭据有效期必须是正安全整数毫秒数");
  }
}
