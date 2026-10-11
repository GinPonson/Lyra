import { randomUUID } from "node:crypto";

import type {
  AgentEvent,
  AgentEventResumePlan,
  AgentSessionSnapshot,
  AgentToolSummary,
  AgentTurnExecutor,
  AgentTurnStreamSignal,
  AgentTurnStreamState,
  FinancialToolRegistry,
  FinancialToolName,
  AgentToolRejectionReason,
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
import { createAgentFinancialToolData } from "./agent-tool-invocation.js";
import { isValidFinancialToolArguments } from "./financial-tool-validation.js";

export interface AgentTurnOrchestratorOptions {
  database: LyraDatabase;
  writeCoordinator: WriteCoordinator;
  registry: FinancialToolRegistry;
  /** 唯一 Genkit 运行时；金融工具直接调用当前编排器。 */
  executor: AgentTurnExecutor;
  /** 每个持久化事件产生时的外发钩子；WebSocket 层也可改用 subscribe 订阅。 */
  onEvent?: (event: AgentEvent) => void;
  now?: () => Date;
}

/** 一个回合期间的活跃编排状态；回合结束后整体丢弃。 */
interface ActiveAgentTurn {
  userMessageId: string;
  streamState: AgentTurnStreamState;
  toolCallsUsed: number;
  abortController: AbortController;
  cancellationRequested: boolean;
  cancellationPromise: Promise<AgentSessionSnapshot> | null;
  /** Genkit 可并行请求多个工具；审计序号与查询按回合串行。 */
  toolQueue: Promise<void>;
  /** 本回合产生的全部统一事件，供调用方推送给 App。 */
  events: AgentEvent[];
}

/** Orchestrator 持有的单会话内存运行态。 */
interface AgentSessionRuntime {
  deviceId: string;
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
 * Agent 回合编排器：把领域回合计划、写仓储和唯一 Genkit 执行器装配成
 * 完整生命周期。授权数据与只读工具调用都在服务器进程内绑定当前回合。
 *
 * 所有 DuckDB 写入通过 WriteCoordinator 串行执行；事件只保存在有界内存缓冲，
 * 由 WebSocket 层消费。orchestrator 不持久化事件正文，也不解释供应商协议。
 */
export class AgentTurnOrchestrator {
  readonly #database: LyraDatabase;
  readonly #writeCoordinator: WriteCoordinator;
  readonly #registry: FinancialToolRegistry;
  readonly #executor: AgentTurnExecutor;
  readonly #now: () => Date;
  readonly #onEvent: ((event: AgentEvent) => void) | null;
  readonly #subscribers = new Map<string, Set<(event: AgentEvent) => void>>();
  readonly #runtimes = new Map<string, AgentSessionRuntime>();
  #cancellationGeneration = 0;
  readonly #lastDeviceCancellation = new Map<string, number>();

  constructor(options: AgentTurnOrchestratorOptions) {
    this.#database = options.database;
    this.#writeCoordinator = options.writeCoordinator;
    this.#registry = options.registry;
    this.#executor = options.executor;
    this.#now = options.now ?? (() => new Date());
    this.#onEvent = options.onEvent ?? null;
  }

  /**
   * 从持久化快照打开一个可编排会话；重复打开返回同一内存运行态。
   *
   * 进程启动时应先恢复遗留的 running/cancelling 会话。此处拒绝未经恢复的
   * 活跃状态，避免重复运行上一进程中断的模型回合。
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
      deviceId: record.deviceId,
      snapshot: record.snapshot,
      lastEventSequence: record.lastEventSequence,
      bufferedEvents: [],
      turn: null,
    });
    return record.snapshot;
  }

  /** 采纳已经持久化的回合边界状态，例如关闭会话。 */
  adoptSession(nextSession: AgentSessionSnapshot): void {
    const runtime = this.#requireRuntime(nextSession.id);
    if (runtime.turn !== null || nextSession.revision <= runtime.snapshot.revision) {
      throw this.#stateConflict("Agent 会话当前不能采纳新状态");
    }
    runtime.snapshot = nextSession;
  }

  /**
   * 运行一个完整用户回合并阻塞到供应商终态持久化完成。
   *
   * 执行器异常视同回合失败，不把底层错误文本交给客户端；取消由 cancelTurn
   * 中止执行器，回合仍经由统一终态路径收口。
   */
  async startTurn(
    sessionId: string,
    content: string,
    authorize?: () => Promise<void>,
  ): Promise<AgentTurnRunResult> {
    // Capture before any await: logout must also invalidate turns still being opened/persisted.
    const admittedGeneration = this.#cancellationGeneration;
    await this.openSession(sessionId);
    const runtime = this.#requireRuntime(sessionId);
    if (runtime.snapshot.status === "closed") {
      throw this.#stateConflict("此会话已关闭或归档，请新建 Lyra 会话继续");
    }
    if (runtime.turn !== null) {
      throw this.#stateConflict("Agent 会话已有进行中的回合");
    }
    await authorize?.();
    if ((this.#lastDeviceCancellation.get(runtime.deviceId) ?? 0) > admittedGeneration) {
      throw new ApplicationError({
        code: "DEVICE_CREDENTIAL_INVALID", message: "登录会话已退出，请重新登录",
        statusCode: 401, retryable: false,
      });
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
      cancellationRequested: false,
      cancellationPromise: null,
      toolQueue: Promise.resolve(),
      events: [],
    };
    runtime.turn = turn;
    this.#bufferEvent(runtime, plan.event);

    try {
      if ((this.#lastDeviceCancellation.get(runtime.deviceId) ?? 0) > admittedGeneration) {
        await this.cancelTurn(sessionId);
        throw new Error("登录会话在回合启动期间退出");
      }
      await this.#executor({
        session: plan.session,
        adapterInput: plan.adapterInput,
        toolDefinitions: this.#registry.availableToolDefinitions(),
        executeTool: (request) => {
          if (runtime.turn !== turn || turn.abortController.signal.aborted) {
            return Promise.reject(new Error("Agent 回合已结束"));
          }
          return this.executeAuthorizedToolCall({ sessionId: plan.session.id, ...request });
        },
        emitSignal: (signal) => {
          if (runtime.turn !== turn) return;
          if (turn.cancellationRequested && signal.kind !== "message_failed"
            && signal.kind !== "turn_settled") return;
          this.#applySignal(runtime, turn, signal);
        },
        abortSignal: turn.abortController.signal,
      });
    } catch {
      // 执行器或模型请求异常；用户已请求取消时按取消收口，
      // 其余按失败收口，均不把底层错误文本暴露给客户端。
      this.#applySignalIfUnsettled(runtime, turn, {
        kind: "message_failed",
        outcome: turn.abortController.signal.aborted ? "cancelled" : "failed",
      });
      this.#applySignalIfUnsettled(runtime, turn, { kind: "turn_settled" });
    }

    // 模型结束后先收敛已开始的工具，防止工具事件落到下一回合。
    await turn.toolQueue;
    // 取消请求可能仍在等待写队列；终态必须使用已持久化的会话状态。
    // 取消写入失败时仍中断模型，并尝试按当前 running 状态记录失败。
    await turn.cancellationPromise?.catch(() => undefined);
    if (turn.cancellationRequested) {
      turn.streamState = {
        ...turn.streamState,
        hasAuthoritativeSnapshot: false,
        failureOutcome: runtime.snapshot.status === "cancelling" ? "cancelled" : "failed",
        settled: true,
      };
    }
    if (!turn.streamState.settled) {
      this.#applySignalIfUnsettled(runtime, turn, { kind: "message_failed", outcome: "failed" });
      this.#applySignalIfUnsettled(runtime, turn, { kind: "turn_settled" });
    }
    // 工具事件只推进全局计数器；结算事件必须接续全局序号而非流状态的本地序号。
    turn.streamState = { ...turn.streamState, lastEventSequence: runtime.lastEventSequence };
    const finalized = finalizeAgentTurnStream({
      state: turn.streamState,
      session: runtime.snapshot,
      finishedAt: this.#now().toISOString(),
    });
    try {
      if (isTurnCompletionPlan(finalized)) {
        await this.#enqueue(() => persistAgentTurnCompletion(this.#database, finalized));
      } else {
        await this.#enqueue(() => persistAgentTurnFailure(this.#database, finalized));
      }
    } catch (error: unknown) {
      // 结算落库失败时必须释放内存中的活跃回合：否则 startTurn/cancelTurn/
      // closeAgentSessionForRequest 会永久以“已有进行中的回合”拒绝，形成只能
      // 靠重启恢复的毒丸会话。同时丢弃缓存运行时，让后续请求重新读取数据库
      // 的权威状态，而不是继续使用与库不一致的内存快照。错误本身照常上抛。
      runtime.turn = null;
      this.#runtimes.delete(sessionId.trim());
      throw error;
    }
    runtime.snapshot = finalized.session;
    runtime.lastEventSequence = finalized.event.sequence;
    this.#bufferEvent(runtime, finalized.event);
    runtime.turn = null;
    return { snapshot: runtime.snapshot, events: turn.events };
  }

  /**
   * 请求取消当前回合：先排队持久化 cancelling，立即中止模型执行器。
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
    if (turn.cancellationPromise !== null) return turn.cancellationPromise;
    if (turn.streamState.settled || turn.streamState.hasAuthoritativeSnapshot) {
      throw this.#stateConflict("Agent 会话没有进行中的回合");
    }
    turn.cancellationRequested = true;
    turn.cancellationPromise = (async () => {
      try {
        if (runtime.snapshot.status === "running") {
          const cancelling = transitionAgentSession(runtime.snapshot, "cancelling");
          await this.#enqueue(() => persistAgentSessionCancelling(this.#database, {
            session: cancelling,
            cancelledAt: this.#now().toISOString(),
          }));
          runtime.snapshot = cancelling;
        }
        return runtime.snapshot;
      } finally {
        // 即使持久化失败，退出登录也必须中断模型和工具等待。
        turn.abortController.abort();
      }
    })();
    turn.abortController.abort();
    return turn.cancellationPromise;
  }

  /** 登录会话撤销后，取消属于该设备的全部活跃模型回合。 */
  async cancelDeviceTurns(deviceId: string): Promise<void> {
    this.#lastDeviceCancellation.set(deviceId, ++this.#cancellationGeneration);
    const turns = [...this.#runtimes.values()].filter((runtime) => (
      runtime.deviceId === deviceId && runtime.turn !== null
      && (runtime.turn.cancellationPromise !== null
        || (!runtime.turn.streamState.settled && !runtime.turn.streamState.hasAuthoritativeSnapshot))
    ));
    await Promise.allSettled(turns.map((runtime) => this.cancelTurn(runtime.snapshot.id)));
  }

  /**
   * 执行一个由当前回合绑定的只读金融工具，并持久化完整调用记录。
   *
   * 工具绑定当前回合的已持久化用户消息；未知或未配置工具直接形成 rejected
   * 终态与最小审计事件。handler 异常以 failed 终态持久化后原样抛出，由
   * Genkit 运行时只向客户端提供稳定错误状态。
   */
  async executeAuthorizedToolCall(input: {
    sessionId: string;
    toolName: string;
    args: unknown;
    abortSignal?: AbortSignal;
    requestId?: string | null;
  }): Promise<{ data: unknown }> {
    const runtime = this.#requireRuntime(input.sessionId);
    const turn = runtime.turn;
    if (turn === null) throw this.#stateConflict("Agent 会话当前没有可执行工具的回合");
    const operation = turn.toolQueue.then(async () => {
      if (runtime.turn !== turn || turn.abortController.signal.aborted
        || runtime.snapshot.status !== "running" || turn.cancellationRequested) {
        throw this.#stateConflict("Agent 会话当前没有可执行工具的回合");
      }
      return this.#executeTool(runtime, turn, input);
    });
    turn.toolQueue = operation.then(() => undefined, () => undefined);
    return operation;
  }

  async #executeTool(
    runtime: AgentSessionRuntime,
    turn: ActiveAgentTurn,
    input: { toolName: string; args: unknown; abortSignal?: AbortSignal; requestId?: string | null },
  ): Promise<{ data: unknown }> {
    const executionSignal = input.abortSignal
      ? AbortSignal.any([turn.abortController.signal, input.abortSignal])
      : turn.abortController.signal;
    executionSignal.throwIfAborted();
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

    const definition = this.#registry.availableToolDefinitions()
      .find((candidate) => candidate.name === input.toolName);
    if (definition === undefined || !isValidFinancialToolArguments(definition, input.args)) {
      await this.#persistRejection(runtime, {
        messageId, ordinal, requestedToolName: input.toolName,
        reason: "invalid_arguments", occurredAt, requestId: input.requestId ?? null,
      });
      throw new AgentToolPolicyError("金融工具参数不符合公开定义");
    }

    if (ordinal > 32) {
      await this.#persistRejection(runtime, {
        messageId, ordinal, requestedToolName: input.toolName,
        reason: "forbidden_capability", occurredAt, requestId: input.requestId ?? null,
      });
      throw new AgentToolPolicyError("本回合工具调用次数已达上限");
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
      executionSignal.throwIfAborted();
      const result = await raceWithAbort(this.#registry.invoke(input.toolName, input.args, {
        accountScope: runtime.snapshot.accountScope,
        ledgerVersion: runtime.snapshot.ledgerVersion,
        asOf: runtime.snapshot.asOf,
      }), executionSignal);
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
      return { data: createAgentFinancialToolData({
        toolName: input.toolName as FinancialToolName,
        context: {
          accountScope: runtime.snapshot.accountScope,
          ledgerVersion: runtime.snapshot.ledgerVersion,
          asOf: runtime.snapshot.asOf,
        },
        data: result,
      }) };
    } catch (error: unknown) {
      const failure = completeAgentToolCall({
        call: startPlan.call,
        outcome: executionSignal.aborted ? "cancelled" : "failed",
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
      reason: AgentToolRejectionReason;
      occurredAt: string;
      requestId: string | null;
    },
  ): Promise<void> {
    const plan = rejectAgentToolCall({
      session: runtime.snapshot,
      messageId: input.messageId,
      callId: randomUUID(),
      ordinal: input.ordinal,
      requestedToolName: /^[a-z][a-z0-9-]{0,63}$/.test(input.requestedToolName)
        ? input.requestedToolName : "invalid-tool-name",
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

  /**
   * 订阅一个会话的后续持久化事件；只推送新事件，不回放历史缓冲。
   *
   * @returns 幂等退订函数；重复调用安全。
   */
  subscribe(sessionId: string, push: (event: AgentEvent) => void): () => void {
    const normalizedId = sessionId.trim();
    let subscribers = this.#subscribers.get(normalizedId);
    if (subscribers === undefined) {
      subscribers = new Set();
      this.#subscribers.set(normalizedId, subscribers);
    }
    subscribers.add(push);
    return () => {
      const current = this.#subscribers.get(normalizedId);
      if (current === undefined) {
        return;
      }
      current.delete(push);
      if (current.size === 0) {
        this.#subscribers.delete(normalizedId);
      }
    };
  }

  #bufferEvent(runtime: AgentSessionRuntime, event: AgentEvent): void {
    runtime.bufferedEvents.push(event);
    runtime.turn?.events.push(event);
    if (runtime.bufferedEvents.length > MAX_BUFFERED_EVENTS) {
      runtime.bufferedEvents.shift();
    }
    this.#onEvent?.(event);
    const subscribers = this.#subscribers.get(runtime.snapshot.id);
    if (subscribers !== undefined) {
      for (const push of subscribers) {
        push(event);
      }
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

/** 中止等待不可取消的只读查询；不再把迟到结果送给模型或下一回合。 */
function raceWithAbort<T>(operation: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(signal.reason ?? new Error("Agent 回合已取消"));
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
    operation.then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
  });
}
