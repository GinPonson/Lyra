import type {
  AgentToolCallRecord,
  AgentToolSummary,
} from "../domain/agent/index.js";

/** App 会话流可展示的工具调用状态，不包含内部授权判定。 */
export interface PublicAgentToolCall {
  id: string;
  messageId: string;
  ordinal: number;
  toolName: string;
  status: AgentToolCallRecord["status"];
  argumentsSummary: AgentToolSummary;
  resultSummary: AgentToolSummary | null;
  ledgerVersion: number;
  durationMs: number | null;
  errorCode: AgentToolCallRecord["errorCode"];
  startedAt: string;
  completedAt: string | null;
}

/**
 * 创建 App 会话页使用的稳定工具调用列表。
 *
 * 参数和结果只使用领域层验证后的扁平有界摘要。会话 ID 由路由上下文确定，
 * 授权判定只用于服务端策略与审计，因此两者均不进入公开记录。
 *
 * @param calls - 同一 Agent 会话内的规范工具调用记录。
 * @returns 按开始时间、消息内顺序和调用 ID 稳定排序的独立公开列表。
 */
export function createPublicAgentToolCallList(
  calls: readonly AgentToolCallRecord[],
): readonly PublicAgentToolCall[] {
  return calls
    .toSorted((left, right) => (
      left.startedAt.localeCompare(right.startedAt)
      || left.ordinal - right.ordinal
      || left.id.localeCompare(right.id)
    ))
    .map((call) => ({
      id: call.id,
      messageId: call.messageId,
      ordinal: call.ordinal,
      toolName: call.toolName,
      status: call.status,
      argumentsSummary: cloneSummary(call.argumentsSummary),
      resultSummary: call.resultSummary === null
        ? null
        : cloneSummary(call.resultSummary),
      ledgerVersion: call.ledgerVersion,
      durationMs: call.durationMs,
      errorCode: call.errorCode,
      startedAt: call.startedAt,
      completedAt: call.completedAt,
    }));
}

/** 复制动态摘要，避免客户端侧修改污染仓储快照。 */
function cloneSummary(summary: AgentToolSummary): AgentToolSummary {
  return Object.fromEntries(Object.entries(summary));
}
