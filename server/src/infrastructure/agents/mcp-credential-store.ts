import { AgentMcpCredentialError } from "../../domain/agent/index.js";
import {
  revokeAgentMcpCredential,
  type AgentMcpCredentialRecord,
} from "./mcp-session-credential.js";

/**
 * 单进程短期 MCP 凭据哈希存储。
 *
 * 明文令牌从不进入本存储；服务重启会自然清空全部记录并使受控 Agent 子进程
 * 重新授权，避免把回合级凭据扩张成长期数据库秘密。
 */
export class AgentMcpCredentialStore {
  readonly #recordsByTokenHash = new Map<string, AgentMcpCredentialRecord>();

  /** 保存签发器生成的哈希记录；重复 token hash 明确拒绝。 */
  add(record: AgentMcpCredentialRecord): void {
    assertTokenHash(record.tokenHash);
    if (this.#recordsByTokenHash.has(record.tokenHash)) {
      throw new AgentMcpCredentialError("invalid_token");
    }
    this.#recordsByTokenHash.set(record.tokenHash, copyRecord(record));
  }

  /** 按 HTTP Bearer 的 SHA-256 查找独立副本。 */
  findByTokenHash(tokenHash: string): AgentMcpCredentialRecord | null {
    if (!/^[a-f0-9]{64}$/u.test(tokenHash)) {
      return null;
    }
    const record = this.#recordsByTokenHash.get(tokenHash);
    return record === undefined ? null : copyRecord(record);
  }

  /** 回存认证产生的 lastUsedAt 或撤销状态，不允许替换凭据身份。 */
  update(record: AgentMcpCredentialRecord): void {
    const existing = this.#recordsByTokenHash.get(record.tokenHash);
    if (
      existing === undefined
      || existing.id !== record.id
      || existing.sessionId !== record.sessionId
      || existing.issuedAt !== record.issuedAt
    ) {
      throw new AgentMcpCredentialError("invalid_token");
    }
    this.#recordsByTokenHash.set(record.tokenHash, copyRecord(record));
  }

  /** 回合结束或会话释放时撤销该会话全部活动凭据。 */
  revokeSession(sessionId: string, now: Date): number {
    let revokedCount = 0;
    for (const [tokenHash, record] of this.#recordsByTokenHash) {
      if (record.sessionId !== sessionId || record.status === "revoked") {
        continue;
      }
      this.#recordsByTokenHash.set(
        tokenHash,
        revokeAgentMcpCredential(record, sessionId, now),
      );
      revokedCount += 1;
    }
    return revokedCount;
  }
}

/** 凭据存储只接受签发器生成的标准 SHA-256。 */
function assertTokenHash(tokenHash: string): void {
  if (!/^[a-f0-9]{64}$/u.test(tokenHash)) {
    throw new AgentMcpCredentialError("invalid_token");
  }
}

/** 深复制账户范围，避免调用方修改存储中的授权口径。 */
function copyRecord(record: AgentMcpCredentialRecord): AgentMcpCredentialRecord {
  return {
    ...record,
    accountScope: record.accountScope.kind === "all"
      ? { kind: "all" }
      : { kind: "account", account: record.accountScope.account },
  };
}
