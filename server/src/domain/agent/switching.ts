import type { AccountScope } from "../analytics/index.js";

/** Lyra 第一阶段支持的服务端 Agent。 */
export type AgentProvider = "claude" | "pi" | "opencode";

/** Agent 会话只允许在 idle 回合边界切换。 */
export type AgentSessionStatus = "idle" | "running" | "cancelling" | "closed";

/** Agent 切换可由客户端稳定处理的预期错误码。 */
export type AgentSwitchErrorCode =
  | "AGENT_SWITCH_REQUIRES_TURN_BOUNDARY"
  | "AGENT_HANDOFF_CONTEXT_TOO_LARGE"
  | "AGENT_UNAVAILABLE";

/** 回合边界或目标供应商状态阻止了 Agent 切换。 */
export class AgentSwitchError extends Error {
  /**
   * @param code - 客户端稳定错误码。
   * @param message - 不包含 CLI 命令、路径或凭据的安全说明。
   * @param internalReason - 仅供服务端日志使用的探测原因，传输层不得返回。
   */
  constructor(
    readonly code: AgentSwitchErrorCode,
    message: string,
    readonly internalReason: string | null = null,
  ) {
    super(message);
    this.name = "AgentSwitchError";
  }
}

/** 供应商探测后的可用状态；不可用必须保留明确原因。 */
export type AgentProviderAvailability =
  | { provider: AgentProvider; status: "available" }
  | { provider: AgentProvider; status: "unavailable"; reason: string };

/** 可迁移的规范化会话消息，不包含模型隐藏推理或供应商内部状态。 */
export interface NormalizedAgentMessage {
  id: string;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  createdAt: string;
  /** 助手消息的持久终态；用户和系统消息无需设置。 */
  status?: "completed" | "failed" | "cancelled";
  /** 失败与取消时供客户端稳定分支的错误码，不保存底层供应商错误。 */
  errorCode?: string | null;
}

/** 持久化的 Agent 会话快照。 */
export interface AgentSessionSnapshot {
  id: string;
  provider: AgentProvider;
  status: AgentSessionStatus;
  accountScope: AccountScope;
  ledgerVersion: number;
  asOf: string;
  revision: number;
  /** 已完成回合的规范化摘要；不包含隐藏推理或供应商缓存。 */
  summary?: string | null;
  messages: readonly NormalizedAgentMessage[];
}

/** 新适配器启动 handoff 时接收的规范化上下文。 */
export interface AgentHandoffContext {
  fromProvider: AgentProvider;
  toProvider: AgentProvider;
  accountScope: AccountScope;
  ledgerVersion: number;
  asOf: string;
  summary: string | null;
  omittedMessageCount: number;
  messages: readonly NormalizedAgentMessage[];
}

/** 一次合法 Agent 切换产生的新快照和 handoff 输入。 */
export interface AgentSwitchPlan {
  nextSession: AgentSessionSnapshot;
  handoff: AgentHandoffContext;
}

/** 账户范围变化后需要原子固化的新会话快照和显式上下文消息。 */
export interface AgentAccountScopeChangePlan {
  nextSession: AgentSessionSnapshot;
  contextMessage: NormalizedAgentMessage;
}

/** 账本更新后需要原子固化的新会话快照和显式上下文消息。 */
export interface AgentLedgerBaselineRefreshPlan {
  nextSession: AgentSessionSnapshot;
  contextMessage: NormalizedAgentMessage;
}

/**
 * 创建绑定首个数据基线的空 Agent 会话。
 *
 * 创建不会启动模型回合或子进程；调用方应先持久化返回快照，再建立私有工作
 * 目录。用户选择的供应商不可用时明确失败，禁止静默切换到其他 Agent。
 */
export function createAgentSession(input: {
  sessionId: string;
  provider: AgentProvider;
  availability: AgentProviderAvailability;
  accountScope: AccountScope;
  ledgerVersion: number;
  asOf: string;
}): AgentSessionSnapshot {
  const sessionId = requireText(input.sessionId, "Agent 会话 ID");
  if (input.availability.provider !== input.provider) {
    throw new Error("Agent 可用性结果与会话供应商不一致");
  }
  if (input.availability.status === "unavailable") {
    throw new AgentSwitchError(
      "AGENT_UNAVAILABLE",
      `Agent ${input.provider} 当前不可用`,
      input.availability.reason,
    );
  }
  if (!Number.isSafeInteger(input.ledgerVersion) || input.ledgerVersion < 0) {
    throw new RangeError("Agent 初始账本版本必须是非负安全整数");
  }
  return {
    id: sessionId,
    provider: input.provider,
    status: "idle",
    accountScope: normalizeAccountScope(input.accountScope),
    ledgerVersion: input.ledgerVersion,
    asOf: requireText(input.asOf, "Agent 初始数据截止时间"),
    revision: 1,
    summary: null,
    messages: [],
  };
}

/**
 * 在完整回合边界生成 Agent 切换计划。
 *
 * @param session - 当前已固化的规范化会话快照。
 * @param target - 用户明确选择的目标 Agent。
 * @param availability - 目标 Agent 的最新探测结果。
 * @param switchMessageId - 将持久化的切换标记消息 ID。
 * @param switchedAt - UTC ISO 时间戳，由应用层时钟提供。
 * @returns 新会话快照和目标适配器 handoff 上下文。
 * @throws AgentSwitchError 当回合未结束或目标不可用时抛出。
 * @throws RangeError 当目标相同或切换输入无效时抛出。
 */
export function prepareAgentSwitch(
  session: AgentSessionSnapshot,
  target: AgentProvider,
  availability: AgentProviderAvailability,
  switchMessageId: string,
  switchedAt: string,
  maximumContextCharacters = 32_000,
): AgentSwitchPlan {
  if (session.status !== "idle") {
    throw new AgentSwitchError(
      "AGENT_SWITCH_REQUIRES_TURN_BOUNDARY",
      `Agent 只能在完整回合边界切换，当前状态为 ${session.status}`,
    );
  }
  if (session.provider === target) {
    throw new RangeError(`当前会话已经使用 ${target}`);
  }
  if (availability.provider !== target) {
    throw new Error(
      `Agent 可用性结果与目标不一致: target=${target} probe=${availability.provider}`,
    );
  }
  if (availability.status === "unavailable") {
    throw new AgentSwitchError(
      "AGENT_UNAVAILABLE",
      `Agent ${target} 当前不可用`,
      availability.reason,
    );
  }
  if (switchMessageId.trim().length === 0 || switchedAt.trim().length === 0) {
    throw new RangeError("Agent 切换消息 ID 和时间戳不能为空");
  }

  const switchMessage: NormalizedAgentMessage = {
    id: switchMessageId,
    role: "system",
    content: (
      `Agent 已从 ${session.provider} 切换到 ${target}；`
      + `账户范围与账本版本 ${session.ledgerVersion} 保持不变。`
    ),
    createdAt: switchedAt,
  };
  const nextMessages = [...session.messages, switchMessage];
  const handoff = buildBoundedAgentHandoff({
    fromProvider: session.provider,
    toProvider: target,
    accountScope: session.accountScope,
    ledgerVersion: session.ledgerVersion,
    asOf: session.asOf,
    summary: session.summary ?? null,
    omittedMessageCount: 0,
    // 目标 Agent 必须收到与持久化快照相同的显式切换边界，不能只靠结构字段推断。
    messages: nextMessages,
  }, maximumContextCharacters);
  return {
    nextSession: {
      ...session,
      provider: target,
      revision: session.revision + 1,
      messages: nextMessages,
    },
    handoff,
  };
}

/**
 * 在完整回合边界显式切换 Agent 会话的账户范围和账本基线。
 *
 * 旧消息仍保留用于对话连续性，但新增 system 消息划清前后数据口径；旧摘要
 * 可能包含原账户数字，因此必须失效。revision 递增会同步使此前签发的 MCP
 * 单会话凭据失效，下一回合只能使用新范围重新签发。
 */
export function prepareAgentAccountScopeChange(input: {
  session: AgentSessionSnapshot;
  accountScope: AccountScope;
  ledgerVersion: number;
  asOf: string;
  messageId: string;
  changedAt: string;
}): AgentAccountScopeChangePlan {
  if (input.session.status !== "idle") {
    throw new AgentSwitchError(
      "AGENT_SWITCH_REQUIRES_TURN_BOUNDARY",
      `Agent 账户范围只能在完整回合边界切换，当前状态为 ${input.session.status}`,
    );
  }
  const nextScope = normalizeAccountScope(input.accountScope);
  if (sameAccountScope(input.session.accountScope, nextScope)) {
    throw new RangeError("Agent 会话已经使用目标账户范围");
  }
  if (!Number.isSafeInteger(input.ledgerVersion) || input.ledgerVersion < 0) {
    throw new RangeError("Agent 新账本版本必须是非负安全整数");
  }
  const asOf = requireText(input.asOf, "Agent 新数据截止时间");
  const messageId = requireText(input.messageId, "Agent 账户范围变更消息 ID");
  const changedAt = requireText(input.changedAt, "Agent 账户范围变更时间");
  if (input.session.messages.some((message) => message.id === messageId)) {
    throw new RangeError("Agent 账户范围变更消息 ID 已存在");
  }

  const contextMessage: NormalizedAgentMessage = {
    id: messageId,
    role: "system",
    content: [
      `账户范围已从${formatAccountScope(input.session.accountScope)}`,
      `切换为${formatAccountScope(nextScope)}；`,
      `后续回答使用账本版本 ${input.ledgerVersion}，截止时间 ${asOf}。`,
      "此前回答仍按其原始账户范围和数据基线理解。",
    ].join(""),
    createdAt: changedAt,
  };
  return {
    nextSession: {
      ...input.session,
      accountScope: nextScope,
      ledgerVersion: input.ledgerVersion,
      asOf,
      revision: input.session.revision + 1,
      summary: null,
      messages: [...input.session.messages, contextMessage],
    },
    contextMessage,
  };
}

/**
 * 在账户范围不变时，将已完成的数据更新显式带入 Agent 会话。
 *
 * 该操作只接受严格递增的账本版本。旧消息保留用于复核，但旧摘要可能包含
 * 已过期数字，因此必须清除；revision 递增会使此前签发的 MCP 会话凭据失效。
 */
export function prepareAgentLedgerBaselineRefresh(input: {
  session: AgentSessionSnapshot;
  ledgerVersion: number;
  asOf: string;
  messageId: string;
  refreshedAt: string;
}): AgentLedgerBaselineRefreshPlan {
  if (input.session.status !== "idle") {
    throw new AgentSwitchError(
      "AGENT_SWITCH_REQUIRES_TURN_BOUNDARY",
      `Agent 数据基线只能在完整回合边界刷新，当前状态为 ${input.session.status}`,
    );
  }
  if (!Number.isSafeInteger(input.ledgerVersion) || input.ledgerVersion < 0) {
    throw new RangeError("Agent 新账本版本必须是非负安全整数");
  }
  if (input.ledgerVersion <= input.session.ledgerVersion) {
    throw new RangeError("Agent 数据基线只能刷新到更高的账本版本");
  }
  const asOf = requireText(input.asOf, "Agent 新数据截止时间");
  const messageId = requireText(input.messageId, "Agent 数据基线刷新消息 ID");
  const refreshedAt = requireText(input.refreshedAt, "Agent 数据基线刷新时间");
  if (input.session.messages.some((message) => message.id === messageId)) {
    throw new RangeError("Agent 数据基线刷新消息 ID 已存在");
  }

  const contextMessage: NormalizedAgentMessage = {
    id: messageId,
    role: "system",
    content: [
      `账户范围保持为${formatAccountScope(input.session.accountScope)}；`,
      `账本数据已从版本 ${input.session.ledgerVersion} 更新到版本 ${input.ledgerVersion}，`,
      `截止时间 ${asOf}。`,
      "后续回答使用新基线，此前回答仍按原始数据基线理解。",
    ].join(""),
    createdAt: refreshedAt,
  };
  return {
    nextSession: {
      ...input.session,
      ledgerVersion: input.ledgerVersion,
      asOf,
      revision: input.session.revision + 1,
      summary: null,
      messages: [...input.session.messages, contextMessage],
    },
    contextMessage,
  };
}

/**
 * 按字符预算保留连续的最近完整消息，并用已固化摘要覆盖被省略的旧消息。
 *
 * 本函数不截断单条消息，也不现场让模型编造摘要。预算不足且没有既有摘要，
 * 或最新一条完整消息本身无法容纳时，切换必须显式失败并先更新会话摘要。
 */
export function buildBoundedAgentHandoff(
  context: AgentHandoffContext,
  maximumContextCharacters: number,
): AgentHandoffContext {
  if (!Number.isSafeInteger(maximumContextCharacters) || maximumContextCharacters <= 0) {
    throw new RangeError("Agent handoff 字符预算必须是正安全整数");
  }
  const normalizedSummary = context.summary?.trim() || null;
  let remaining = maximumContextCharacters - (normalizedSummary?.length ?? 0);
  if (remaining < 0) {
    throw handoffTooLarge("会话摘要超过 handoff 字符预算");
  }

  const selected: NormalizedAgentMessage[] = [];
  let omittedMessageCount = 0;
  for (let index = context.messages.length - 1; index >= 0; index -= 1) {
    const message = context.messages[index];
    if (message === undefined) {
      continue;
    }
    if (message.content.length > remaining) {
      omittedMessageCount = index + 1;
      break;
    }
    selected.unshift(message);
    remaining -= message.content.length;
  }

  if (omittedMessageCount > 0 && (normalizedSummary === null || selected.length === 0)) {
    throw handoffTooLarge("切换上下文需要先生成覆盖旧消息的会话摘要");
  }
  return {
    ...context,
    summary: normalizedSummary,
    omittedMessageCount,
    messages: omittedMessageCount === 0 ? context.messages : selected,
  };
}

/** 生成不包含消息正文或供应商详情的稳定切换错误。 */
function handoffTooLarge(internalReason: string): AgentSwitchError {
  return new AgentSwitchError(
    "AGENT_HANDOFF_CONTEXT_TOO_LARGE",
    "Agent 切换上下文过大，需要先更新会话摘要",
    internalReason,
  );
}

/** 复制并校验模型不可控制的目标账户范围。 */
function normalizeAccountScope(scope: AccountScope): AccountScope {
  if (scope.kind === "all") {
    return { kind: "all" };
  }
  const account = requireText(scope.account, "Agent 目标账户");
  return { kind: "account", account };
}

/** 判断两个显式账户范围是否相同。 */
function sameAccountScope(left: AccountScope, right: AccountScope): boolean {
  return left.kind === right.kind
    && (left.kind === "all" || (right.kind === "account" && left.account === right.account));
}

/** 生成人类可复核、不会省略范围类型的上下文标签。 */
function formatAccountScope(scope: AccountScope): string {
  return scope.kind === "all" ? "全部活跃账户" : `单账户 ${scope.account}`;
}

/** 规范上下文字段并拒绝空字符串。 */
function requireText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}
