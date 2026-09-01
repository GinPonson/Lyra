/** 三个 Agent 适配器统一输出的流式事件类型。 */
export const AGENT_EVENT_TYPES = [
  "session.started",
  "message.started",
  "message.delta",
  "reasoning.delta",
  "tool.started",
  "tool.completed",
  "approval.required",
  "usage.updated",
  "message.completed",
  "message.failed",
  "session.completed",
] as const;

/** Agent WebSocket 事件名。 */
export type AgentEventType = typeof AGENT_EVENT_TYPES[number];

/** 发送给 Flutter 的统一 Agent 事件信封。 */
export interface AgentEvent {
  type: AgentEventType;
  sessionId: string;
  messageId: string | null;
  /** 在单个 session 内严格递增，从 1 开始。 */
  sequence: number;
  occurredAt: string;
  payload: Readonly<Record<string, unknown>>;
}

/** 创建单个事件需要的适配器输出。 */
export interface EmitAgentEventInput {
  type: AgentEventType;
  messageId: string | null;
  occurredAt: string;
  payload: Readonly<Record<string, unknown>>;
}

/** WebSocket 重连时的事件续传结果；缺口必须改取规范会话快照。 */
export type AgentEventResumePlan =
  | { mode: "events"; events: readonly AgentEvent[] }
  | {
      mode: "snapshot_required";
      reason: "client_ahead" | "event_gap" | "mixed_session";
    };

/**
 * 为一个 Agent 会话分配单调递增事件序号。
 *
 * 该类不负责 WebSocket 发送或持久化，避免把传输机制耦合进 Agent 领域。会话
 * 恢复时可用已经持久化的最后序号初始化，下一事件继续递增。
 */
export class AgentEventSequencer {
  private sequence: number;

  /**
   * @param sessionId - 当前会话的稳定 ID。
   * @param lastSequence - 已经发出或持久化的最后序号；新会话使用 0。
   */
  constructor(
    private readonly sessionId: string,
    lastSequence = 0,
  ) {
    if (sessionId.trim().length === 0) {
      throw new RangeError("sessionId 不能为空");
    }
    if (!Number.isSafeInteger(lastSequence) || lastSequence < 0) {
      throw new RangeError("lastSequence 必须是非负安全整数");
    }
    this.sequence = lastSequence;
  }

  /**
   * 生成下一条统一事件。
   *
   * @param input - 适配器已经规范化的事件类型、消息、时间和负载。
   * @returns 带会话 ID 和下一序号的新事件。
   */
  next(input: EmitAgentEventInput): AgentEvent {
    if (input.occurredAt.trim().length === 0) {
      throw new RangeError("Agent 事件时间戳不能为空");
    }
    this.sequence += 1;
    return {
      type: input.type,
      sessionId: this.sessionId,
      messageId: input.messageId,
      sequence: this.sequence,
      occurredAt: input.occurredAt,
      payload: input.payload,
    };
  }
}

/**
 * 判断同一会话的相邻事件是否连续。
 *
 * 返回 false 时客户端必须重新获取会话快照，不能猜测或拼接缺失内容。
 */
export function areAgentEventsContiguous(previous: AgentEvent, next: AgentEvent): boolean {
  return previous.sessionId === next.sessionId
    && next.sequence === previous.sequence + 1;
}

/**
 * 判定 Flutter 重连后能否从有界内存缓冲区安全续传 Agent 事件。
 *
 * 只有缓冲区完整覆盖 `(afterSequence, lastPersistedSequence]` 且严格连续时才
 * 返回增量事件。客户端游标超前、缓冲区缺口或混入其他会话都要求重新获取
 * 规范会话快照，绝不补造 delta 或猜测最终文本。
 */
export function planAgentEventResume(input: {
  sessionId: string;
  afterSequence: number;
  lastPersistedSequence: number;
  bufferedEvents: readonly AgentEvent[];
}): AgentEventResumePlan {
  const sessionId = input.sessionId.trim();
  if (sessionId.length === 0) {
    throw new RangeError("Agent 事件恢复 sessionId 不能为空");
  }
  requireNonNegativeSequence(input.afterSequence, "Agent 客户端事件游标");
  requireNonNegativeSequence(input.lastPersistedSequence, "Agent 服务端事件游标");
  if (input.afterSequence > input.lastPersistedSequence) {
    return { mode: "snapshot_required", reason: "client_ahead" };
  }
  if (input.bufferedEvents.some((event) => event.sessionId !== sessionId)) {
    return { mode: "snapshot_required", reason: "mixed_session" };
  }
  if (input.afterSequence === input.lastPersistedSequence) {
    return { mode: "events", events: [] };
  }

  const events = input.bufferedEvents.filter((event) => (
    event.sequence > input.afterSequence
    && event.sequence <= input.lastPersistedSequence
  ));
  let expectedSequence = input.afterSequence + 1;
  for (const event of events) {
    if (event.sequence !== expectedSequence) {
      return { mode: "snapshot_required", reason: "event_gap" };
    }
    expectedSequence += 1;
  }
  if (expectedSequence !== input.lastPersistedSequence + 1) {
    return { mode: "snapshot_required", reason: "event_gap" };
  }
  return { mode: "events", events };
}

/** 校验事件游标不会发生负数、浮点或超出安全整数范围。 */
function requireNonNegativeSequence(value: number, label: string): void {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new RangeError(`${label}必须是非负安全整数`);
  }
}
