import { openAICompatible } from "@genkit-ai/compat-oai";
import { generateMiddleware, genkit, modelRef } from "genkit";
import type { Genkit, MessageData, ModelArgument } from "genkit";

import type { AgentTurnExecutor, FinancialToolName } from "../../domain/agent/index.js";
import { buildAgentSystemContext } from "../../domain/agent/index.js";
import { createAgentAvailabilitySnapshot } from "../../application/agent-availability.js";
import type { AgentAvailabilitySnapshot } from "../../application/agent-availability.js";
import { isValidFinancialToolArguments } from "../../application/financial-tool-validation.js";

const DEFAULT_TIMEOUT_MS = 120_000;
const DEFAULT_MAX_TURNS = 10;
const MAX_HISTORY_CHARACTERS = 120_000;

/** 单个 Genkit 实例，模型地址与密钥由部署环境决定。 */
export interface GenkitRuntime {
  executor: AgentTurnExecutor;
  /** 配置状态不发出真实模型请求，也不把端点、密钥暴露给 App。 */
  availability: () => Promise<AgentAvailabilitySnapshot>;
}

/**
 * 只创建一个 Genkit 助手。兼容 OpenAI Chat Completions 的后端可以更换，
 * 客户端始终只看到 Lyra；不注册 CLI、MCP、Shell、网络浏览或文件系统工具。
 */
export function createGenkitRuntimeFromEnvironment(
  environment: NodeJS.ProcessEnv = process.env,
): GenkitRuntime {
  const baseURL = environment.LYRA_GENKIT_BASE_URL?.trim();
  const model = environment.LYRA_GENKIT_MODEL?.trim();
  const apiKey = environment.LYRA_GENKIT_API_KEY?.trim();
  const turnTimeoutMilliseconds = positiveInteger(
    environment.LYRA_AGENT_TURN_TIMEOUT_MS, DEFAULT_TIMEOUT_MS, 1_000, 600_000,
  );
  const maxTurns = positiveInteger(environment.LYRA_AGENT_MAX_TURNS, DEFAULT_MAX_TURNS, 1, 32);
  if (!baseURL || !model || !apiKey) {
    return {
      executor: async () => { throw new Error("Lyra 模型尚未配置"); },
      availability: async () => createAgentAvailabilitySnapshot(false, new Date()),
    };
  }
  const endpoint = new URL(baseURL);
  if (!["http:", "https:"].includes(endpoint.protocol)
    || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
    throw new RangeError("LYRA_GENKIT_BASE_URL 必须是无凭据、查询参数和片段的 HTTP(S) 地址");
  }
  const ai = genkit({
    name: "Lyra",
    promptDir: null,
    plugins: [openAICompatible({
      name: "lyra",
      baseURL: endpoint.toString().replace(/\/$/, ""),
      apiKey,
      timeout: turnTimeoutMilliseconds,
      maxRetries: 0,
    })],
  });
  return {
    executor: createGenkitTurnExecutor({
      ai,
      model: modelRef({
        name: `lyra/${model}`,
        info: {
          supports: {
            multiturn: true, tools: true, systemRole: true,
            media: false, output: ["text"],
          },
        },
      }),
      turnTimeoutMilliseconds,
      maxTurns,
    }),
    availability: async () => createAgentAvailabilitySnapshot(true, new Date()),
  };
}

/**
 * 使用 Genkit 的真实工具循环，保持 Lyra 原有规范消息/工具事件契约。
 * 注入 ai/model 方便用 Genkit 本身的假模型验证循环，不需要外部账号或费用。
 */
export function createGenkitTurnExecutor(options: {
  ai: Genkit;
  model: ModelArgument;
  turnTimeoutMilliseconds?: number;
  maxTurns?: number;
}): AgentTurnExecutor {
  const timeoutMs = options.turnTimeoutMilliseconds ?? DEFAULT_TIMEOUT_MS;
  const maxTurns = options.maxTurns ?? DEFAULT_MAX_TURNS;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0
    || !Number.isSafeInteger(maxTurns) || maxTurns < 1 || maxTurns > 32) {
    throw new RangeError("Genkit 回合超时和工具轮次配置无效");
  }
  return async (input) => {
    const timeout = new AbortController();
    const timer = setTimeout(() => timeout.abort(new Error("Genkit turn timed out")), timeoutMs);
    timer.unref();
    const signal = AbortSignal.any([input.abortSignal, timeout.signal]);
    let active = true;
    let totalTokens = 0;
    let hasUsage = false;
    try {
      signal.throwIfAborted();
      const definitions = new Map(input.toolDefinitions.map((definition) => [definition.name, definition]));
      const messages = createGenkitMessages(input.session.messages);
      const system = buildAgentSystemContext({
        accountScope: input.session.accountScope,
        ledgerVersion: input.session.ledgerVersion,
        asOf: input.session.asOf,
        availableTools: input.toolDefinitions.map((definition) => definition.name as FinancialToolName),
      });
      const tools = input.toolDefinitions.map((definition) => options.ai.dynamicTool({
        name: definition.name,
        description: definition.description,
        // Same canonical JSON Schema used by REST and server-side validation.
        inputJsonSchema: { ...definition.inputSchema, required: [...(definition.inputSchema.required ?? [])] },
      }, async (args: unknown) => {
        signal.throwIfAborted();
        if (!active) throw new Error("Agent 回合已结束");
        const result = await input.executeTool({ toolName: definition.name, args, abortSignal: signal });
        signal.throwIfAborted();
        return result.data;
      }));

      // The model middleware sees unknown/invalid requests before Genkit rejects
      // them, so denied requests also go through Lyra's durable audit boundary.
      const boundary = generateMiddleware({ name: "lyra-financial-boundary" }, () => ({
        model: async (request, context, next) => {
          signal.throwIfAborted();
          const response = await next(request, context);
          signal.throwIfAborted();
          if (!active) throw new Error("Agent 回合已结束");
          const tokens = response.usage?.totalTokens
            ?? ((response.usage?.inputTokens ?? 0) + (response.usage?.outputTokens ?? 0));
          if (response.usage && Number.isFinite(tokens) && tokens >= 0) {
            totalTokens += tokens;
            hasUsage = true;
          }
          for (const part of response.message?.content ?? []) {
            if (!part.toolRequest) continue;
            const definition = definitions.get(part.toolRequest.name);
            if (!definition || !isValidFinancialToolArguments(definition, part.toolRequest.input)) {
              await input.executeTool({ toolName: part.toolRequest.name, args: part.toolRequest.input, abortSignal: signal });
              throw new Error("Agent 工具授权边界未拒绝无效请求");
            }
          }
          return response;
        },
      }));

      const generated = options.ai.generateStream({
        model: options.model,
        system,
        messages,
        tools,
        use: [boundary()],
        maxTurns,
        abortSignal: signal,
        stepName: "lyra-financial-assistant",
      });
      // Attach rejection handlers immediately: both response and stream can fail.
      const consumed = (async () => {
        for await (const chunk of generated.stream) {
          if (!active || signal.aborted) break;
          if (chunk.role !== "model") continue;
          if (chunk.reasoning) input.emitSignal({ kind: "reasoning_delta", delta: "active" });
          if (chunk.text) input.emitSignal({ kind: "message_delta", delta: chunk.text });
        }
      })();
      const operation = Promise.all([generated.response, consumed]);
      const [response] = await raceWithAbort(operation, signal);
      signal.throwIfAborted();
      if (response.finishReason !== "stop" || !response.text.trim()) {
        throw new Error("Genkit 未返回完整回答");
      }
      if (hasUsage) input.emitSignal({ kind: "usage_updated", totalTokens, totalCost: null });
      input.emitSignal({ kind: "message_snapshot", content: response.text, stopReason: "end_turn" });
    } catch {
      input.emitSignal({
        kind: "message_failed",
        outcome: input.abortSignal.aborted ? "cancelled" : "failed",
      });
    } finally {
      active = false;
      clearTimeout(timer);
      timeout.abort();
      input.emitSignal({ kind: "turn_settled" });
    }
  };
}

/** 历史按规范角色传递；失败的部分回答不冒充模型完成的回答。 */
function createGenkitMessages(
  messages: Parameters<AgentTurnExecutor>[0]["session"]["messages"],
): MessageData[] {
  if (messages.reduce((length, message) => length + message.content.length, 0) > MAX_HISTORY_CHARACTERS) {
    throw new RangeError("会话上下文过长，请新建 Lyra 会话");
  }
  return messages.flatMap((message): MessageData[] => {
    if (message.role === "tool") return [];
    if (message.role === "assistant" && message.status && message.status !== "completed") return [];
    return [{
      role: message.role === "assistant" ? "model" : message.role,
      content: [{ text: message.content }],
    }];
  });
}

function positiveInteger(value: string | undefined, fallback: number, min: number, max: number): number {
  if (!value) return fallback;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < min || parsed > max) {
    throw new RangeError("Lyra Agent 超时或最大轮次配置超出允许范围");
  }
  return parsed;
}

/** 即使模型插件忽略 AbortSignal，也能结束服务器回合并抑制迟到的结果。 */
function raceWithAbort<T>(operation: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(signal.reason ?? new Error("Agent 回合已取消"));
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
    operation.then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
  });
}
