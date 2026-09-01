import { AgentEventSequencer } from "./events.js";
import type { AgentEvent } from "./events.js";
import type { AgentSessionSnapshot } from "./switching.js";
import {
  completeAgentTurn,
  failAgentTurn,
} from "./turn.js";
import type {
  AgentTurnCompletionPlan,
  AgentTurnFailureOutcome,
  AgentTurnFailurePlan,
} from "./turn.js";

/** 三个供应商适配器共用的消息流信号子集。 */
export type AgentTurnStreamSignal =
  | { kind: "message_delta"; delta: string }
  | { kind: "reasoning_delta"; delta: string }
  | { kind: "message_snapshot"; content: string; stopReason: string }
  | { kind: "usage_updated"; totalTokens: number; totalCost: number }
  | { kind: "message_failed"; outcome: AgentTurnFailureOutcome }
  | { kind: "turn_settled" };

/** 单个逻辑回合的可恢复流状态；不保存 reasoning 或供应商原始帧。 */
export interface AgentTurnStreamState {
  sessionId: string;
  messageId: string;
  visibleContent: string;
  /** 收到 message_end 后为 true，此时 visibleContent 是供应商权威快照。 */
  hasAuthoritativeSnapshot: boolean;
  failureOutcome: AgentTurnFailureOutcome | null;
  settled: boolean;
  totalTokens: number | null;
  totalCost: number | null;
  lastEventSequence: number;
}

/** 应用一个流信号后的新状态和可立即推送给 App 的事件。 */
export interface AgentTurnStreamUpdate {
  state: AgentTurnStreamState;
  event: AgentEvent | null;
}

/** 建立绑定会话、消息和已持久化事件序号的空流状态。 */
export function createAgentTurnStreamState(input: {
  session: AgentSessionSnapshot;
  messageId: string;
  lastEventSequence: number;
}): AgentTurnStreamState {
  if (input.session.status !== "running") {
    throw new RangeError("只有 running 会话可以建立 Agent 消息流");
  }
  const messageId = requireText(input.messageId, "Agent 流消息 ID");
  if (!Number.isSafeInteger(input.lastEventSequence) || input.lastEventSequence < 0) {
    throw new RangeError("Agent 流事件序号必须是非负安全整数");
  }
  return {
    sessionId: input.session.id,
    messageId,
    visibleContent: "",
    hasAuthoritativeSnapshot: false,
    failureOutcome: null,
    settled: false,
    totalTokens: null,
    totalCost: null,
    lastEventSequence: input.lastEventSequence,
  };
}

/**
 * 累积一个供应商消息信号并按需生成统一实时事件。
 *
 * 文本 delta 可见且会累积；reasoning 只生成不含原文的即时 active 状态事件，
 * 既不写入状态也不发送给 App。message snapshot 覆盖增量聚合结果，因为它是
 * 供应商的最终权威可见文本。settled 后拒绝任何新信号，防止跨回合拼接。
 */
export function applyAgentTurnStreamSignal(input: {
  state: AgentTurnStreamState;
  session: AgentSessionSnapshot;
  signal: AgentTurnStreamSignal;
  occurredAt: string;
}): AgentTurnStreamUpdate {
  if (input.state.sessionId !== input.session.id) {
    throw new RangeError("Agent 流状态与会话不匹配");
  }
  if (input.state.settled) {
    throw new RangeError("Agent 流已经 settled，不能继续追加信号");
  }
  const occurredAt = requireText(input.occurredAt, "Agent 流事件时间");
  let state: AgentTurnStreamState = { ...input.state };
  let eventInput: {
    type: "message.delta" | "reasoning.delta" | "usage.updated";
    payload: Readonly<Record<string, unknown>>;
  } | null = null;

  switch (input.signal.kind) {
    case "message_delta": {
      if (state.failureOutcome !== null || state.hasAuthoritativeSnapshot) {
        throw new RangeError("Agent 终态快照或失败后不能追加文本增量");
      }
      if (input.signal.delta.length === 0) {
        return { state, event: null };
      }
      state = { ...state, visibleContent: state.visibleContent + input.signal.delta };
      eventInput = { type: "message.delta", payload: { delta: input.signal.delta } };
      break;
    }
    case "reasoning_delta": {
      if (state.failureOutcome !== null || state.hasAuthoritativeSnapshot) {
        throw new RangeError("Agent 终态快照或失败后不能追加 reasoning 增量");
      }
      if (input.signal.delta.length === 0) {
        return { state, event: null };
      }
      // App 只需要驱动“思考中”动效，模型隐藏推理不得进入传输层或重连缓冲。
      eventInput = { type: "reasoning.delta", payload: { state: "active" } };
      break;
    }
    case "message_snapshot":
      if (state.failureOutcome !== null) {
        throw new RangeError("Agent 失败后不能接受成功消息快照");
      }
      state = {
        ...state,
        visibleContent: input.signal.content,
        hasAuthoritativeSnapshot: true,
      };
      break;
    case "usage_updated":
      requireNonNegativeNumber(input.signal.totalTokens, "Agent total tokens");
      requireNonNegativeNumber(input.signal.totalCost, "Agent total cost");
      state = {
        ...state,
        totalTokens: input.signal.totalTokens,
        totalCost: input.signal.totalCost,
      };
      eventInput = {
        type: "usage.updated",
        payload: {
          total_tokens: input.signal.totalTokens,
          total_cost: input.signal.totalCost,
        },
      };
      break;
    case "message_failed":
      if (state.hasAuthoritativeSnapshot) {
        throw new RangeError("Agent 成功消息快照后不能改写为失败");
      }
      if (state.failureOutcome !== null && state.failureOutcome !== input.signal.outcome) {
        throw new RangeError("Agent 流收到冲突的失败终态");
      }
      state = { ...state, failureOutcome: input.signal.outcome };
      break;
    case "turn_settled":
      state = { ...state, settled: true };
      break;
  }

  if (eventInput === null) {
    return { state, event: null };
  }
  const event = new AgentEventSequencer(
    state.sessionId,
    state.lastEventSequence,
  ).next({
    type: eventInput.type,
    messageId: state.messageId,
    occurredAt,
    payload: {
      ...eventInput.payload,
      provider: input.session.provider,
      ledger_version: input.session.ledgerVersion,
    },
  });
  return {
    state: { ...state, lastEventSequence: event.sequence },
    event,
  };
}

/**
 * settled 后复用既有原子计划生成唯一持久终态。
 *
 * 成功必须有非空权威快照；否则按明确失败收口。取消必须由调用方先把会话状态
 * 转为 cancelling，避免从供应商退出码猜测用户意图。
 */
export function finalizeAgentTurnStream(input: {
  state: AgentTurnStreamState;
  session: AgentSessionSnapshot;
  finishedAt: string;
}): AgentTurnCompletionPlan | AgentTurnFailurePlan {
  if (input.state.sessionId !== input.session.id) {
    throw new RangeError("Agent 流状态与会话不匹配");
  }
  if (!input.state.settled) {
    throw new RangeError("Agent 流尚未 settled，不能持久化终态");
  }
  if (input.state.failureOutcome !== null) {
    return failAgentTurn({
      session: input.session,
      messageId: input.state.messageId,
      outcome: input.state.failureOutcome,
      partialContent: input.state.visibleContent,
      finishedAt: input.finishedAt,
      lastEventSequence: input.state.lastEventSequence,
    });
  }
  if (!input.state.hasAuthoritativeSnapshot || input.state.visibleContent.trim().length === 0) {
    return failAgentTurn({
      session: input.session,
      messageId: input.state.messageId,
      outcome: "failed",
      partialContent: input.state.visibleContent,
      finishedAt: input.finishedAt,
      lastEventSequence: input.state.lastEventSequence,
    });
  }
  return completeAgentTurn({
    session: input.session,
    messageId: input.state.messageId,
    content: input.state.visibleContent,
    completedAt: input.finishedAt,
    lastEventSequence: input.state.lastEventSequence,
  });
}

/** 去除边界空白并拒绝空协议标识。 */
function requireText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}

/** 校验流式 usage 为非负有限数值。 */
function requireNonNegativeNumber(value: number, label: string): void {
  if (!Number.isFinite(value) || value < 0) {
    throw new RangeError(`${label}必须是非负有限数值`);
  }
}
