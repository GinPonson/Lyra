import type { FastifyInstance } from "fastify";
import websocket from "@fastify/websocket";

import type { AgentEvent } from "../domain/agent/index.js";
import type { AgentTurnOrchestrator } from "../application/index.js";
import type { PairedDeviceAuthRecord } from "../infrastructure/security/device-auth.js";

/** WebSocket 事件路由所需的装配依赖。 */
export interface AgentEventsRouteOptions {
  orchestrator: AgentTurnOrchestrator;
  authenticateDevice: (token: string) => Promise<PairedDeviceAuthRecord>;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu;

interface SubscribeMessage {
  type: "subscribe";
  sessionId: string;
  afterSequence: number;
}

/** 解析客户端订阅帧；协议不符返回 null。 */
function parseSubscribeMessage(raw: string | null): SubscribeMessage | null {
  if (raw === null) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw) as unknown;
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) {
    return null;
  }
  const record = parsed as Record<string, unknown>;
  if (record["type"] !== "subscribe") {
    return null;
  }
  const sessionId = record["sessionId"];
  const afterSequence = record["afterSequence"];
  if (typeof sessionId !== "string" || !UUID_PATTERN.test(sessionId)) {
    return null;
  }
  if (
    typeof afterSequence !== "number"
    || !Number.isSafeInteger(afterSequence)
    || afterSequence < 0
  ) {
    return null;
  }
  return { type: "subscribe", sessionId, afterSequence };
}

const websocketRegisteredApps = new WeakSet<FastifyInstance>();

/**
 * 注册 GET /v1/events WebSocket 事件表面。
 *
 * 连接先通过设备访问令牌认证；客户端以 `subscribe` 帧绑定会话与游标，服务端
 * 返回缓冲续传或要求重新取快照，随后实时推送该会话的持久化事件。本路由不
 * 缓存、不补造事件：序号缺口一律由客户端重新获取规范会话快照解决。
 */
export async function registerAgentEventsRoute(
  app: FastifyInstance,
  options: AgentEventsRouteOptions,
): Promise<void> {
  if (!websocketRegisteredApps.has(app)) {
    await app.register(websocket);
    websocketRegisteredApps.add(app);
  }

  app.get("/v1/events", { websocket: true }, (socket, request) => {
    let unsubscribe: (() => void) | null = null;
    let boundSessionId: string | null = null;

    const push = (event: AgentEvent): void => {
      if (socket.readyState === socket.OPEN) {
        socket.send(JSON.stringify({ type: "event", event }));
      }
    };

    const authenticate = async (): Promise<boolean> => {
      const header = request.headers.authorization;
      const token = typeof header === "string" && header.startsWith("Bearer ")
        ? header.slice("Bearer ".length)
        : null;
      if (token === null) {
        return false;
      }
      try {
        await options.authenticateDevice(token);
        return true;
      } catch {
        return false;
      }
    };
    void authenticate().then((authorized) => {
      if (!authorized) {
        socket.close(4001, "unauthorized");
      }
    });

    socket.on("message", (data: unknown) => {
      const message = parseSubscribeMessage(toText(data));
      if (message === null) {
        socket.send(JSON.stringify({
          type: "error",
          code: "INVALID_ARGUMENT",
          message: "WebSocket 订阅帧无效",
        }));
        return;
      }
      let resume: ReturnType<typeof options.orchestrator.resumeEvents>;
      try {
        resume = options.orchestrator.resumeEvents(
          message.sessionId,
          message.afterSequence,
        );
      } catch {
        // 运行时未在内存(如进程重启后重连)不可补造事件;按既有契约要求
        // 客户端重取权威快照。socket 回调内抛出异常会击穿整个服务进程。
        socket.send(JSON.stringify({
          type: "subscribed",
          mode: "snapshot_required",
          reason: "SESSION_NOT_RESUMED",
        }));
        return;
      }
      if (boundSessionId !== message.sessionId && unsubscribe !== null) {
        unsubscribe();
      }
      unsubscribe = options.orchestrator.subscribe(message.sessionId, push);
      boundSessionId = message.sessionId;
      socket.send(JSON.stringify(
        resume.mode === "events"
          ? { type: "subscribed", mode: "events", events: resume.events }
          : { type: "subscribed", mode: "snapshot_required", reason: resume.reason },
      ));
    });

    socket.on("close", () => {
      unsubscribe?.();
      unsubscribe = null;
    });
  });
}

/** 把 ws 的 RawData 归一为文本；二进制帧按 null 拒绝。 */
function toText(data: unknown): string | null {
  if (typeof data === "string") {
    return data;
  }
  if (Array.isArray(data)) {
    return Buffer.concat(data).toString("utf8");
  }
  if (data instanceof Uint8Array) {
    return Buffer.from(data).toString("utf8");
  }
  if (data instanceof ArrayBuffer) {
    return Buffer.from(data).toString("utf8");
  }
  return null;
}
