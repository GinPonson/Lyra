import type {
  AgentToolContext,
  FinancialToolName,
} from "../domain/agent/index.js";

/** 交给模型的金融数据包；provenance 只从服务端会话上下文生成。 */
export interface AgentFinancialToolData {
  kind: "lyra_financial_data";
  toolName: FinancialToolName;
  provenance: AgentToolContext & { instructionBoundary: "data_only" };
  data: unknown;
}

/** 不接受模型指定的账户范围、版本或截止时间。 */
export function createAgentFinancialToolData(input: {
  toolName: FinancialToolName;
  context: AgentToolContext;
  data: unknown;
}): AgentFinancialToolData {
  return {
    kind: "lyra_financial_data",
    toolName: input.toolName,
    provenance: {
      accountScope: input.context.accountScope.kind === "all"
        ? { kind: "all" }
        : { kind: "account", account: input.context.accountScope.account },
      ledgerVersion: input.context.ledgerVersion,
      asOf: input.context.asOf,
      instructionBoundary: "data_only",
    },
    data: input.data,
  };
}
