import { randomUUID } from "node:crypto";

import type {
  AgentEvent,
  AgentEventResumePlan,
  AgentProvider,
  AgentSessionSnapshot,
  AgentToolSummary,
  AgentTurnStreamSignal,
  AgentTurnStreamState,
  FinancialToolRegistry,
} from "../domain/agent/index.js";
import {
  AgentToolPolicyError,
  FINANCIAL_TOOL_NAMES,
  applyAgentTurnStreamSignal,
  createAgentTurnStreamState,
  finalizeAgentTurnStream,
  planAgentEventResume,
  rejectAgentToolCall,
  startAgentToolCall,
  startAgentTurn,
  transitionAgentSession,
  completeAgentToolCall,
} from "../domain/agent/index.js";
import { issueAgentMcpCredential } from "../infrastructure/agents/index.js";
import type { AgentMcpCredentialStore } from "../infrastructure/agents/index.js";
import {
  loadAgentSessionRuntime,
  persistAgentSessionCancelling,
  persistAgentTurnCompletion,
  persistAgentTurnFailure,
  persistAgentTurnStart,
} from "../infrastructure/db/agent-session-repository.js";
import {
  persistAgentToolCallCompletion,
  persistAgentToolCallRejection,
  persistAgentToolCallStart,
} from "../infrastructure/db/agent-tool-call-repository.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";
import { ApplicationError } from "./app-error.js";
import type { WriteCoordinator } from "./write-coordinator.js";

/**
 * 单个供应商回合的执行边界。
 *
 * Orchestrator 只认统一流信号：执行器把 Claude/Pi/OpenCode 的协议帧翻译为
 * 信号并保证以 `turn_settled` 结束；取消通过 abortSignal 请求，执行器据此
 * 中断子进程并发送 `message_failed(cancelled)`。工具调用不经过执行器，由
 * CLI 通过 MCP HTTP 独立到达 orchestrator。
 */
export type AgentTurnExecutor = (input: {
  session: AgentSessionSnapshot;
  adapterInput: { messageId: string; content: string };
  mcp: { endpoint: string; bearer: string };
  emitSignal: (signal: AgentTurnStreamSignal) => void;
  abortSignal: AbortSignal;
}) => Promise<void>;

export interface AgentTurnOrchestratorOptions {
  database: LyraDatabase;
  writeCoordinator: WriteCoordinator;
  mcpCredentials: AgentMcpCredentialStore;
  registry: FinancialToolRegistry;
  /** 三个供应商各自的回合执行器；orchestrator 不解释供应商协议帧。 */
  executors: Readonly<Record<AgentProvider, AgentTurnExecutor>>;
  /** 供应商子进程访问 Lyra MCP 的回环端点。 */
  mcpEndpoint: string;
  mcpCredentialTtlMilliseconds: number;
  now?: () => Date;
}

/** 一个回合期间的活跃编排状态；回合结束后整体丢弃。 */
interface ActiveAgentTurn {
  userMessageId: string;
  streamState: AgentTurnStreamState;
  toolCallsUsed: number;
  abortController: AbortController;
  /** 本回合产生的全部统一事件，供调用方推送给 App。 */
  events: AgentEvent[];
}

/** Orchestrator 持有的单会话内存运行态。 */
interface AgentSessionRuntime {
  snapshot: AgentSessionSnapshot;
  lastEventSequence: number;
  bufferedEvents: AgentEvent[];
  turn: ActiveAgentTurn | null;
}

/** 一次已完成回合的编排结果。 */
export interface AgentTurnRunResult {
  snapshot: AgentSessionSnapshot;
  events: AgentEvent[];
}

/** 会话事件续传请求的结果。 */
export type AgentEventResume = AgentEventResumePlan;

/** 事件缓冲上限；溢出后丢弃最旧事件，续传方会收到 snapshot_required。 */
const MAX_BUFFERED_EVENTS = 500;

/**
 * Agent 回合编排器：把已验证的领域回合计划、写仓储、MCP 短期凭据和供应商
 * 执行器装配成完整回合生命周期。
 *
 * 所有 DuckDB 写入通过 WriteCoordinator 串行执行；事件只保存在有界内存缓冲，
 * 由 WebSocket 层消费。orchestrator 不持久化事件正文，也不解释供应商协议。
 */
export class AgentTurnOrchestrator {
  readonly #database: LyraDatabase;
  readonly #writeCoordinator: WriteCoordinator;
  readonly #mcpCredentials: AgentMcpCredentialStore;
  readonly #registry: FinancialToolRegistry;
  readonly #executors: Readonly<Record<AgentProvider, AgentTurnExecutor>>;
  readonly #mcpEndpoint: string;
  readonly #mcpCredentialTtlMilliseconds: number;
  readonly #now: () => Date;
  readonly #runtimes = new Map<string, AgentSessionRuntime>();

  constructor(options: AgentTurnOrchestratorOptions) {
    if (options.mcpEndpoint.trim().length === 0) {
      throw new RangeError("Agent MCP 端点不能为空");
    }
    if (
      !Number.isSafeInteger(options.mcpCredentialTtlMilliseconds)
      || options.mcpCredentialTtlMilliseconds <= 0
    ) {
      throw new RangeError("Agent MCP 凭据有效期必须是正安全整数");
    }
    this.#database = options.database;
    this.#writeCoordinator = options.writeCoordinator;
    this.#mcpCredentials = options.mcpCredentials;
    this.#registry = options.registry;
    this.#executors = options.executors;
    this.#mcpEndpoint = options.mcpEndpoint;
    this.#mcpCredentialTtlMilliseconds = options.mcpCredentialTtlMilliseconds;
    this.#now = options.now ?? (() => new Date());
  }

  /**
   * 从持久化快照打开一个可编排会话；重复打开返回同一内存运行态。
   *
   * 进程重启后遗留的 running/cancelling 会话没有可恢复的供应商子进程，本
   * 方法直接拒绝编排用途，等待后续恢复流程收口，不伪造状态。
   */
  async openSession(sessionId: string): Promise<AgentSessionSnapshot> {
    const normalizedId = sessionId.trim();
    const existing = this.#runtimes.get(normalizedId);
    if (existing !== undefined) {
      return existing.snapshot;
    }
    const record = await loadAgentSessionRuntime(this.#database, normalizedId);
    if (record === null) {
      throw new ApplicationError({
        code: "AGENT_SESSION_STATE_CONFLICT",
        message: "Agent 会话不存在",
        statusCode: 404,
        retryable: false,
      });
    }
    if (record.snapshot.status === "running" || record.snapshot.status === "cancelling") {
      throw new ApplicationError({
        code: "AGENT_SESSION_STATE_CONFLICT",
        message: "Agent 会话存在未完成的回合，请刷新后重试",
        statusCode: 409,
        retryable: false,
      });
    }
    this.#runtimes.set(normalizedId, {
      snapshot: record.snapshot,
      lastEventSequence: record.lastEventSequence,
      bufferedEvents: [],
      turn: null,
    });
    return record.snapshot;
  }

  /**
   * 运行一个完整用户回合并阻塞到供应商终态持久化完成。
   *
   * 执行器异常视同回合失败，不把底层错误文本交给客户端；取消由 cancelTurn
   * 中止执行器，回合仍经由统一终态路径收口。
   */
  async startTurn(sessionId: string, content: string): Promise<AgentTurnRunResult> {
    const runtime = this.#requireRuntime(sessionId);
    if (runtime.turn !== null) {
      throw this.#stateConflict("Agent 会话已有进行中的回合");
    }
    const startedAt = this.#now().toISOString();
    const plan = startAgentTurn({
      session: runtime.snapshot,
      messageId: randomUUID(),
      content,
      startedAt,
      lastEventSequence: runtime.lastEventSequence,
    });
    await this.#enqueue(() => persistAgentTurnStart(this.#database, plan));

    runtime.snapshot = plan.session;
    runtime.lastEventSequence = plan.event.sequence;
    const issued = issueAgentMcpCredential({
      session: plan.session,
      now: this.#now(),
      ttlMilliseconds: this.#mcpCredentialTtlMilliseconds,
    });
    this.#mcpCredentials.add(issued.record);
    // 流式事件与终态助手消息共用同一 ID；工具调用则绑定已持久化的用户消息。
    const assistantMessageId = randomUUID();
    const turn: ActiveAgentTurn = {
      userMessageId: plan.userMessage.id,
      streamState: createAgentTurnStreamState({
        session: plan.session,
        messageId: assistantMessageId,
        lastEventSequence: plan.event.sequence,
      }),
      toolCallsUsed: 0,
      abortController: new AbortController(),
      events: [],
    };
    runtime.turn = turn;
    this.#bufferEvent(runtime, plan.event);

    try {
      await this.#executors[plan.session.provider]({
        session: plan.session,
        adapterInput: plan.adapterInput,
        mcp: { endpoint: this.#mcpEndpoint, bearer: issued.token },
        emitSignal: (signal) => this.#applySignal(runtime, turn, signal),
        abortSignal: turn.abortController.signal,
      });
    } catch {
      // 执行器抛出代表供应商进程异常退出；仍按失败回合收口，不暴露底层原因。
      this.#applySignalIfUnsettled(runtime, turn, {
        kind: "message_failed",
        outcome: "failed",
      });
      this.#applySignalIfUnsettled(runtime, turn, { kind: "turn_settled" });
    }

    // 工具事件只推进全局计数器；结算事件必须接续全局序号而非流状态的本地序号。
    turn.streamState = { ...turn.streamState, lastEventSequence: runtime.lastEventSequence };
    const finalized = finalizeAgentTurnStream({
      state: turn.streamState,
      session: runtime.snapshot,
      finishedAt: this.#now().toISOString(),
    });
    if (isTurnCompletionPlan(finalized)) {
      await this.#enqueue(() => persistAgentTurnCompletion(this.#database, finalized));
    } else {
      await this.#enqueue(() => persistAgentTurnFailure(this.#database, finalized));
    }
    runtime.snapshot = finalized.session;
    runtime.lastEventSequence = finalized.event.sequence;
    this.#bufferEvent(runtime, finalized.event);
    runtime.turn = null;
    this.#mcpCredentials.revokeSession(sessionId.trim(), this.#now());
    return { snapshot: runtime.snapshot, events: turn.events };
  }

  /**
   * 请求取消当前回合：先持久化 cancelling，再中止供应商执行器。
   *
   * 回合最终状态由 startTurn 的统一收口路径持久化，本方法只负责状态转换与
   * 中断信号，重复取消保持幂等。
   */
  async cancelTurn(sessionId: string): Promise<AgentSessionSnapshot> {
    const runtime = this.#requireRuntime(sessionId);
    const turn = runtime.turn;
    if (turn === null) {
      throw this.#stateConflict("Agent 会话没有进行中的回合");
    }
    if (runtime.snapshot.status === "running") {
      const cancelling = transitionAgentSession(runtime.snapshot, "cancelling");
      await this.#enqueue(() => persistAgentSessionCancelling(this.#database, {
        session: cancelling,
        cancelledAt: this.#now().toISOString(),
      }));
      runtime.snapshot = cancelling;
    }
    turn.abortController.abort();
    return runtime.snapshot;
  }

  /**
   * 执行一次已经过 MCP 认证的金融工具调用，并持久化完整调用记录。
   *
   * 工具绑定当前回合的已持久化用户消息；未知或未配置工具直接形成 rejected
   * 终态与最小审计事件。handler 异常以 failed 终态持久化后原样抛出，由
   * MCP 层转换为稳定协议错误。
   */
  async executeAuthorizedToolCall(input: {
    sessionId: string;
    toolName: string;
    args: Readonly<Record<string, unknown>>;
    requestId?: string | null;
  }): Promise<{ data: unknown }> {
    const runtime = this.#requireRuntime(input.sessionId);
    const turn = runtime.turn;
    if (turn === null || runtime.snapshot.status !== "running") {
      throw this.#stateConflict("Agent 会话当前没有可执行工具的回合");
    }
    const messageId = turn.userMessageId;
    const ordinal = turn.toolCallsUsed + 1;
    turn.toolCallsUsed = ordinal;
    const occurredAt = this.#now().toISOString();

    if (!(FINANCIAL_TOOL_NAMES as readonly string[]).includes(input.toolName)) {
      await this.#persistRejection(runtime, {
        messageId,
        ordinal,
        requestedToolName: input.toolName,
        reason: "unknown_tool",
        occurredAt,
        requestId: input.requestId ?? null,
      });
      throw new AgentToolPolicyError("金融工具不存在或尚未启用");
    }
    if (!this.#configuredToolNames().has(input.toolName)) {
      await this.#persistRejection(runtime, {
        messageId,
        ordinal,
        requestedToolName: input.toolName,
        reason: "unconfigured_tool",
        occurredAt,
        requestId: input.requestId ?? null,
      });
      throw new AgentToolPolicyError("金融工具不存在或尚未启用");
    }

    const startPlan = startAgentToolCall({
      session: runtime.snapshot,
      messageId,
      callId: randomUUID(),
      ordinal,
      toolName: input.toolName,
      argumentsSummary: summarizeFlatScalars(input.args),
      startedAt: occurredAt,
      lastEventSequence: runtime.lastEventSequence,
    });
    await this.#enqueue(() => persistAgentToolCallStart(this.#database, startPlan));
    runtime.lastEventSequence = startPlan.event.sequence;
    this.#bufferEvent(runtime, startPlan.event);

    const startedAtMs = this.#now().getTime();
    try {
      const result = await this.#registry.invoke(input.toolName, input.args, {
        accountScope: runtime.snapshot.accountScope,
        ledgerVersion: runtime.snapshot.ledgerVersion,
        asOf: runtime.snapshot.asOf,
      });
      const completion = completeAgentToolCall({
        call: startPlan.call,
        outcome: "completed",
        resultSummary: summarizeFlatScalars(result),
        durationMs: Math.max(0, this.#now().getTime() - startedAtMs),
        completedAt: this.#now().toISOString(),
        lastEventSequence: runtime.lastEventSequence,
      });
      await this.#enqueue(() => persistAgentToolCallCompletion(this.#database, completion));
      runtime.lastEventSequence = completion.event.sequence;
      this.#bufferEvent(runtime, completion.event);
      return { data: result };
    } catch (error: unknown) {
      const failure = completeAgentToolCall({
        call: startPlan.call,
        outcome: "failed",
        resultSummary: null,
        durationMs: Math.max(0, this.#now().getTime() - startedAtMs),
        completedAt: this.#now().toISOString(),
        lastEventSequence: runtime.lastEventSequence,
      });
      await this.#enqueue(() => persistAgentToolCallCompletion(this.#database, failure));
      runtime.lastEventSequence = failure.event.sequence;
      this.#bufferEvent(runtime, failure.event);
      throw error;
    }
  }

  /** 按客户端游标判定能否从内存缓冲续传事件，缺口一律要求重新取快照。 */
  resumeEvents(sessionId: string, afterSequence: number): AgentEventResume {
    const runtime = this.#requireRuntime(sessionId);
    return planAgentEventResume({
      sessionId: runtime.snapshot.id,
      afterSequence,
      lastPersistedSequence: runtime.lastEventSequence,
      bufferedEvents: runtime.bufferedEvents,
    });
  }

  /** 当前内存运行态的只读快照视图。 */
  peekSession(sessionId: string): AgentSessionSnapshot | null {
    return this.#runtimes.get(sessionId.trim())?.snapshot ?? null;
  }

  #configuredToolNames(): ReadonlySet<string> {
    return new Set(
      this.#registry.availableToolDefinitions().map((definition) => definition.name),
    );
  }

  async #persistRejection(
    runtime: AgentSessionRuntime,
    input: {
      messageId: string;
      ordinal: number;
      requestedToolName: string;
      reason: "unknown_tool" | "unconfigured_tool";
      occurredAt: string;
      requestId: string | null;
    },
  ): Promise<void> {
    const plan = rejectAgentToolCall({
      session: runtime.snapshot,
      messageId: input.messageId,
      callId: randomUUID(),
      ordinal: input.ordinal,
      requestedToolName: input.requestedToolName,
      reason: input.reason,
      rejectedAt: input.occurredAt,
      lastEventSequence: runtime.lastEventSequence,
      requestId: input.requestId,
    });
    await this.#enqueue(() => persistAgentToolCallRejection(this.#database, plan));
    runtime.lastEventSequence = plan.event.sequence;
    this.#bufferEvent(runtime, plan.event);
  }

  #applySignal(
    runtime: AgentSessionRuntime,
    turn: ActiveAgentTurn,
    signal: AgentTurnStreamSignal,
  ): void {
    // 工具事件只推进全局计数器；流事件必须接续全局序号，避免与工具事件冲突。
    turn.streamState = { ...turn.streamState, lastEventSequence: runtime.lastEventSequence };
    const update = applyAgentTurnStreamSignal({
      state: turn.streamState,
      session: runtime.snapshot,
      signal,
      occurredAt: this.#now().toISOString(),
    });
    turn.streamState = update.state;
    if (update.event !== null) {
      runtime.lastEventSequence = update.event.sequence;
      this.#bufferEvent(runtime, update.event);
    }
  }

  /** 执行器异常路径专用：已经 settled 或失败快照后的信号安全忽略。 */
  #applySignalIfUnsettled(
    runtime: AgentSessionRuntime,
    turn: ActiveAgentTurn,
    signal: AgentTurnStreamSignal,
  ): void {
    try {
      this.#applySignal(runtime, turn, signal);
    } catch {
      // 终态信号重复或冲突说明执行器已经给出权威终态，保留原终态。
    }
  }

  #bufferEvent(runtime: AgentSessionRuntime, event: AgentEvent): void {
    runtime.bufferedEvents.push(event);
    runtime.turn?.events.push(event);
    if (runtime.bufferedEvents.length > MAX_BUFFERED_EVENTS) {
      runtime.bufferedEvents.shift();
    }
  }

  #requireRuntime(sessionId: string): AgentSessionRuntime {
    const normalizedId = sessionId.trim();
    const runtime = this.#runtimes.get(normalizedId);
    if (runtime === undefined) {
      throw new ApplicationError({
        code: "AGENT_SESSION_STATE_CONFLICT",
        message: "Agent 会话尚未在编排器中打开",
        statusCode: 409,
        retryable: false,
      });
    }
    return runtime;
  }

  async #enqueue<T>(operation: () => Promise<T>): Promise<T> {
    return this.#writeCoordinator.enqueue(operation);
  }

  #stateConflict(message: string): ApplicationError {
    return new ApplicationError({
      code: "AGENT_SESSION_STATE_CONFLICT",
      message,
      statusCode: 409,
      retryable: false,
    });
  }
}

/** 只提取顶层标量形成有界摘要；嵌套结构不进入持久层。 */
function summarizeFlatScalars(value: unknown): AgentToolSummary {
  const summary: Record<string, string | number | boolean | null> = {};
  if (value === null || typeof value !== "object") {
    return summary;
  }
  for (const [key, entry] of Object.entries(value)) {
    if (
      typeof entry === "string"
      || typeof entry === "number"
      || typeof entry === "boolean"
    ) {
      summary[key] = entry;
    }
  }
  return summary;
}

/** 领域保证 settled 流的终态类型；这里用它区分两个持久化计划的分支。 */
function isTurnCompletionPlan(
  plan: ReturnType<typeof finalizeAgentTurnStream>,
): plan is Parameters<typeof persistAgentTurnCompletion>[1] {
  return plan.event.type === "message.completed";
}
