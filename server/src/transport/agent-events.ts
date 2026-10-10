import type { FastifyInstance } from "fastify";
import websocket from "@fastify/websocket";
import type { WebSocket } from "ws";

import type { AgentEvent } from "../domain/agent/index.js";
import type { AgentTurnOrchestrator } from "../application/index.js";
import type { PairedDeviceAuthRecord } from "../infrastructure/security/device-auth.js";

/** WebSocket 事件路由所需的装配依赖。 */
export interface AgentEventsRouteOptions {
  orchestrator: AgentTurnOrchestrator;
  authenticateDevice: (token: string) => Promise<PairedDeviceAuthRecord>;
}

/** 已撤销设备的事件连接可随退出登录立即关闭。 */
export interface AgentEventConnections {
  disconnectDevice(deviceId: string): void;
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
): Promise<AgentEventConnections> {
  if (!websocketRegisteredApps.has(app)) {
    await app.register(websocket);
    websocketRegisteredApps.add(app);
  }
  const deviceSockets = new Map<string, Set<WebSocket>>();

  app.get("/v1/events", { websocket: true }, (socket, request) => {
    let unsubscribe: (() => void) | null = null;
    let deviceId: string | null = null;
    let queue = Promise.resolve();
    const header = request.headers.authorization;
    const token = typeof header === "string" && header.startsWith("Bearer ")
      ? header.slice("Bearer ".length)
      : null;

    const authenticate = async (): Promise<boolean> => {
      if (token === null || socket.readyState !== socket.OPEN) return false;
      try {
        const device = await options.authenticateDevice(token);
        if (socket.readyState !== socket.OPEN) return false;
        deviceId = device.id;
        const sockets = deviceSockets.get(device.id) ?? new Set<WebSocket>();
        sockets.add(socket);
        deviceSockets.set(device.id, sockets);
        return true;
      } catch {
        socket.close(4001, "unauthorized");
        return false;
      }
    };

    // 首帧必须等待认证完成；不能在异步认证尚未结算时订阅账本事件。
    queue = authenticate().then((authorized) => {
      if (!authorized) socket.close(4001, "unauthorized");
    });
    const push = (event: AgentEvent): void => {
      queue = queue.then(async () => {
        // 长连接同样遵守凭据过期与设备撤销，不沿用连接时的认证结果。
        if (await authenticate()) {
          socket.send(JSON.stringify({ type: "event", event }));
        }
      }).catch(() => { socket.close(1011, "event unavailable"); });
    };

    socket.on("message", (data: unknown) => {
      queue = queue.then(async () => {
        if (!await authenticate()) return;
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
          socket.send(JSON.stringify({
            type: "subscribed",
            mode: "snapshot_required",
            reason: "SESSION_NOT_RESUMED",
          }));
          return;
        }
        // 重复 subscribe 也替换旧订阅，避免同一事件被发送多次。
        unsubscribe?.();
        unsubscribe = options.orchestrator.subscribe(message.sessionId, push);
        socket.send(JSON.stringify(
          resume.mode === "events"
            ? { type: "subscribed", mode: "events", events: resume.events }
            : { type: "subscribed", mode: "snapshot_required", reason: resume.reason },
        ));
      }).catch(() => { socket.close(1011, "subscription unavailable"); });
    });

    socket.on("close", () => {
      unsubscribe?.();
      unsubscribe = null;
      if (deviceId !== null) {
        const sockets = deviceSockets.get(deviceId);
        sockets?.delete(socket);
        if (sockets?.size === 0) deviceSockets.delete(deviceId);
      }
    });
  });

  return {
    disconnectDevice(deviceId: string): void {
      for (const socket of deviceSockets.get(deviceId) ?? []) {
        socket.close(4001, "signed out");
      }
      deviceSockets.delete(deviceId);
    },
  };
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
