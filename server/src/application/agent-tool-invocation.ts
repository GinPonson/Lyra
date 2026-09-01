import type {
  AgentSessionSnapshot,
  FinancialToolName,
  FinancialToolRegistry,
} from "../domain/agent/index.js";
import {
  authenticateAgentMcpCredential,
} from "../infrastructure/agents/index.js";
import type {
  AgentMcpCredentialRecord,
} from "../infrastructure/agents/index.js";

/** 交给 Agent 的金融数据包；provenance 由服务端认证上下文生成。 */
export interface AgentFinancialToolData {
  kind: "lyra_financial_data";
  toolName: FinancialToolName;
  provenance: {
    accountScope: AgentSessionSnapshot["accountScope"];
    ledgerVersion: number;
    asOf: string;
    instructionBoundary: "data_only";
  };
  data: unknown;
}

/** 一次已认证金融工具调用的结果，以及需要回存的凭据使用时间。 */
export interface AuthenticatedAgentToolResult {
  result: AgentFinancialToolData;
  credentialRecord: AgentMcpCredentialRecord;
}

/**
 * 执行来自原生 Agent 进程的单次 MCP 金融工具调用。
 *
 * 凭据认证负责恢复服务端权威的账户范围、账本版本和截止时间；工具注册表
 * 随后移除模型参数中的同名范围字段并执行已配置的只读 handler。因此调用方
 * 不应从 MCP 请求体构造 AgentToolContext，也不应在认证前触发任何金融查询。
 *
 * @param input.session - 当前服务端规范会话快照，必须仍处于同一 running revision。
 * @param input.credentialRecord - 服务端保存的短期 MCP 凭据哈希记录。
 * @param input.credentialToken - MCP Authorization Bearer 中的明文令牌。
 * @param input.registry - 三个 Agent 共用的金融工具注册表。
 * @param input.toolName - MCP 请求的原始工具名。
 * @param input.arguments - 已由具体工具 schema 校验的参数。
 * @param input.now - 服务端接收请求的时间。
 * @returns 工具结果与更新 lastUsedAt 后的凭据记录。
 */
export async function invokeAuthenticatedAgentTool(input: {
  session: AgentSessionSnapshot;
  credentialRecord: AgentMcpCredentialRecord;
  credentialToken: string;
  registry: FinancialToolRegistry;
  toolName: string;
  arguments: Readonly<Record<string, unknown>>;
  now: Date;
}): Promise<AuthenticatedAgentToolResult> {
  const authenticated = authenticateAgentMcpCredential({
    session: input.session,
    record: input.credentialRecord,
    token: input.credentialToken,
    now: input.now,
  });
  const result = await input.registry.invoke(
    input.toolName,
    input.arguments,
    authenticated.context,
  );
  return {
    result: {
      kind: "lyra_financial_data",
      // invoke 已执行同一白名单校验，因此这里的字符串已被收窄为实际金融工具。
      toolName: input.toolName as FinancialToolName,
      provenance: {
        accountScope: authenticated.context.accountScope.kind === "all"
          ? { kind: "all" }
          : {
              kind: "account",
              account: authenticated.context.accountScope.account,
            },
        ledgerVersion: authenticated.context.ledgerVersion,
        asOf: authenticated.context.asOf,
        instructionBoundary: "data_only",
      },
      data: result,
    },
    credentialRecord: authenticated.record,
  };
}
