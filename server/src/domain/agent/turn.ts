import {
  AgentEventSequencer,
} from "./events.js";
import type { AgentEvent } from "./events.js";
import {
  AgentSessionStateError,
  transitionAgentSession,
} from "./lifecycle.js";
import type {
  AgentSessionSnapshot,
  NormalizedAgentMessage,
} from "./switching.js";

/** 用户消息已经固化、可发送给当前供应商适配器的回合起始计划。 */
export interface AgentTurnStartPlan {
  session: AgentSessionSnapshot;
  userMessage: NormalizedAgentMessage;
  event: AgentEvent;
  adapterInput: {
    messageId: string;
    content: string;
  };
}

/** 供应商完整回答已经固化、会话可回到回合边界的成功计划。 */
export interface AgentTurnCompletionPlan {
  session: AgentSessionSnapshot;
  assistantMessage: NormalizedAgentMessage;
  event: AgentEvent;
}

/** 失败或取消已经固化、会话可安全恢复到回合边界的终态计划。 */
export interface AgentTurnFailurePlan {
  session: AgentSessionSnapshot;
  /** 只有供应商已经产生用户可见的部分回答时才持久化消息。 */
  assistantMessage: NormalizedAgentMessage | null;
  event: AgentEvent;
}

/** Agent 回合非成功终态及其稳定客户端错误码。 */
export type AgentTurnFailureOutcome = "failed" | "cancelled";

/**
 * 在 idle 边界固化用户消息并生成统一 `message.started` 事件。
 *
 * 该函数不启动子进程。应用层应先在单写事务中保存 running 快照、用户消息和
 * 事件序号，成功后再把 adapterInput 写入供应商协议；这样进程启动失败也能由
 * 后续明确失败事件收口，而不会出现未记录的模型回合。
 *
 * @param input - 当前会话、客户端生成的消息 ID、用户内容、时间和已持久化序号。
 * @returns 不含凭据和隐藏上下文的确定性回合起始计划。
 */
export function startAgentTurn(input: {
  session: AgentSessionSnapshot;
  messageId: string;
  content: string;
  startedAt: string;
  lastEventSequence: number;
}): AgentTurnStartPlan {
  if (input.session.status !== "idle") {
    throw new AgentSessionStateError(input.session.status, "running");
  }
  const messageId = requireText(input.messageId, "Agent 用户消息 ID");
  const content = requireText(input.content, "Agent 用户消息");
  if (input.session.messages.some((message) => message.id === messageId)) {
    throw new RangeError("Agent 用户消息 ID 已存在");
  }
  const startedAt = requireText(input.startedAt, "Agent 回合开始时间");
  const userMessage: NormalizedAgentMessage = {
    id: messageId,
    role: "user",
    content,
    createdAt: startedAt,
  };
  const running = transitionAgentSession(input.session, "running");
  const session: AgentSessionSnapshot = {
    ...running,
    messages: [...running.messages, userMessage],
  };
  const event = new AgentEventSequencer(
    input.session.id,
    input.lastEventSequence,
  ).next({
    type: "message.started",
    messageId,
    occurredAt: startedAt,
    payload: {
      role: "user",
      provider: input.session.provider,
      ledger_version: input.session.ledgerVersion,
      account_scope: input.session.accountScope,
    },
  });

  return {
    session,
    userMessage,
    event,
    adapterInput: { messageId, content },
  };
}

/**
 * 固化完整助手回答、生成统一完成事件并把 running 会话恢复为 idle。
 *
 * 供应商增量只用于实时展示，不应直接写入规范消息；调用方在收到供应商明确
 * 完成信号后，将聚合后的可见文本与本计划一起原子持久化。失败和取消必须走
 * 独立终态计划，不能用空成功消息掩盖。
 *
 * @param input - running 会话、助手消息身份、完整可见内容、时间和事件序号。
 * @returns idle 会话快照、规范助手消息和不复制正文的完成事件。
 */
export function completeAgentTurn(input: {
  session: AgentSessionSnapshot;
  messageId: string;
  content: string;
  completedAt: string;
  lastEventSequence: number;
}): AgentTurnCompletionPlan {
  if (input.session.status !== "running") {
    throw new AgentSessionStateError(input.session.status, "idle");
  }
  const messageId = requireText(input.messageId, "Agent 助手消息 ID");
  const content = requireText(input.content, "Agent 助手消息");
  if (input.session.messages.some((message) => message.id === messageId)) {
    throw new RangeError("Agent 助手消息 ID 已存在");
  }
  const completedAt = requireText(input.completedAt, "Agent 回合完成时间");
  const assistantMessage: NormalizedAgentMessage = {
    id: messageId,
    role: "assistant",
    content,
    createdAt: completedAt,
    status: "completed",
    errorCode: null,
  };
  const idle = transitionAgentSession(input.session, "idle");
  const session: AgentSessionSnapshot = {
    ...idle,
    messages: [...idle.messages, assistantMessage],
  };
  const event = new AgentEventSequencer(
    input.session.id,
    input.lastEventSequence,
  ).next({
    type: "message.completed",
    messageId,
    occurredAt: completedAt,
    payload: {
      role: "assistant",
      provider: input.session.provider,
      ledger_version: input.session.ledgerVersion,
    },
  });
  return { session, assistantMessage, event };
}

/**
 * 固化供应商失败或用户取消，并把会话恢复到 idle 回合边界。
 *
 * `failed` 只能收口 running 会话，`cancelled` 只能收口已经进入 cancelling 的
 * 会话。非空部分回答会作为明确标记的助手消息保留；空部分不生成虚假消息。
 * 事件只携带稳定错误码和账本基线，不复制正文或底层供应商错误。
 *
 * @param input - 当前会话、助手消息 ID、终态、可选部分回答、时间和事件序号。
 * @returns idle 会话、可选的失败消息及统一 `message.failed` 事件。
 */
export function failAgentTurn(input: {
  session: AgentSessionSnapshot;
  messageId: string;
  outcome: AgentTurnFailureOutcome;
  partialContent: string | null;
  finishedAt: string;
  lastEventSequence: number;
}): AgentTurnFailurePlan {
  const expectedStatus = input.outcome === "failed" ? "running" : "cancelling";
  if (input.session.status !== expectedStatus) {
    throw new AgentSessionStateError(input.session.status, "idle");
  }
  const messageId = requireText(input.messageId, "Agent 助手消息 ID");
  if (input.session.messages.some((message) => message.id === messageId)) {
    throw new RangeError("Agent 助手消息 ID 已存在");
  }
  const finishedAt = requireText(input.finishedAt, "Agent 回合结束时间");
  const errorCode = input.outcome === "failed"
    ? "AGENT_TURN_FAILED"
    : "AGENT_TURN_CANCELLED";
  const partialContent = input.partialContent?.trim() ?? "";
  const assistantMessage: NormalizedAgentMessage | null = partialContent.length === 0
    ? null
    : {
        id: messageId,
        role: "assistant",
        content: partialContent,
        createdAt: finishedAt,
        status: input.outcome,
        errorCode,
      };
  const idle = transitionAgentSession(input.session, "idle");
  const session: AgentSessionSnapshot = {
    ...idle,
    messages: assistantMessage === null
      ? idle.messages
      : [...idle.messages, assistantMessage],
  };
  const event = new AgentEventSequencer(
    input.session.id,
    input.lastEventSequence,
  ).next({
    type: "message.failed",
    messageId,
    occurredAt: finishedAt,
    payload: {
      provider: input.session.provider,
      ledger_version: input.session.ledgerVersion,
      outcome: input.outcome,
      error_code: errorCode,
    },
  });
  return { session, assistantMessage, event };
}

/** 规范用户可见文本并拒绝空输入。 */
function requireText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}
