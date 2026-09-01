import type {
  AgentSessionSnapshot,
  AgentSessionStatus,
} from "./switching.js";

/** Agent 会话状态转换被当前回合边界拒绝。 */
export class AgentSessionStateError extends Error {
  readonly code = "AGENT_SESSION_STATE_CONFLICT" as const;

  /** @param from - 当前状态；@param to - 请求的新状态。 */
  constructor(
    readonly from: AgentSessionStatus,
    readonly to: AgentSessionStatus,
  ) {
    super(`Agent 会话不能从 ${from} 转换为 ${to}`);
    this.name = "AgentSessionStateError";
  }
}

/** 第一阶段会话生命周期允许的显式状态边。 */
const ALLOWED_TRANSITIONS: Readonly<Record<AgentSessionStatus, readonly AgentSessionStatus[]>> = {
  idle: ["running", "closed"],
  running: ["cancelling", "idle"],
  cancelling: ["idle"],
  closed: [],
};

/**
 * 生成 Agent 会话的下一持久化快照。
 *
 * `idle→running` 开始回合；运行中可完成回到 idle 或先进入 cancelling；只有
 * idle 会话可关闭。相同状态重放保持幂等，不递增 revision。调用方必须先把
 * 供应商终态事件和消息固化，再把 running/cancelling 转回 idle。
 *
 * @param session - 当前不可变会话快照。
 * @param target - 运行编排器请求的新状态。
 * @returns revision 已递增的新快照，或相同状态时返回原对象。
 */
export function transitionAgentSession(
  session: AgentSessionSnapshot,
  target: AgentSessionStatus,
): AgentSessionSnapshot {
  if (session.status === target) {
    return session;
  }
  if (!ALLOWED_TRANSITIONS[session.status].includes(target)) {
    throw new AgentSessionStateError(session.status, target);
  }
  return {
    ...session,
    status: target,
    revision: session.revision + 1,
  };
}
